# app/db/candidates.py — Handles all database CRUD operations for candidates, including necessary joins with the jobs table.

import json
from typing import Optional, List, Dict
from psycopg2.extras import Json
from app.db.connection import get_cursor


def _generate_atomic_candidate_id(cur, job_id: int) -> str:
    cur.execute(
        """
        INSERT INTO job_sequences (job_id, next_seq)
        VALUES (%s, 1)
        ON CONFLICT (job_id) DO UPDATE SET next_seq = job_sequences.next_seq + 1
        RETURNING next_seq;
    """,
        (job_id,),
    )
    seq = cur.fetchone()["next_seq"]
    return f"{job_id}{seq:02d}"


def save_candidate(
    job_id: int, result: dict, resume_filename: str = "", sharepoint_link: str = "", source: str = ""
) -> int:
    match = result.get("function_1_resume_jd_matching", {})
    extract = result.get("function_2_resume_data_extraction", {})
    personal = extract.get("personal_information", {})
    employment = extract.get("current_employment", {})
    career = extract.get("career_metrics", {})

    def _param(key: str) -> dict:
        """Extract {status, summary} for a single ParameterMatch field."""
        obj = match.get(key, {})
        return {
            "status": obj.get("status", ""),
            "summary": obj.get("summary", ""),
        }

    match_breakdown = {
        "experience": _param("experience"),
        "education": _param("education"),
        "location": _param("location"),
        "project_history": _param("project_history_relevance"),
        "tools_used": _param("tools_used"),
        "certifications": _param("certifications"),
    }

    with get_cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM candidates WHERE resume_filename = %s AND job_id = %s LIMIT 1",
            (resume_filename, job_id),
        )
        existing = cur.fetchone()

        if existing:
            cur.execute(
                """
                UPDATE candidates SET
                    full_name = %s, email = %s, phone = %s, location = %s,
                    current_title = %s, current_company = %s, total_experience = %s,
                    match_score = %s, match_breakdown = %s::jsonb,
                    sharepoint_link = COALESCE(NULLIF(%s, ''), sharepoint_link),
                    source = COALESCE(NULLIF(%s, ''), source),
                    raw_json = %s, screened_at = NOW()
                WHERE id = %s RETURNING id
            """,
                (
                    personal.get("full_name", "Unknown"),
                    personal.get("email", ""),
                    personal.get("phone", ""),
                    personal.get("location", ""),
                    employment.get("current_job_title", ""),
                    employment.get("current_organization", ""),
                    career.get("total_experience_in_years", 0.0),
                    match.get("overall_match_score", 0),
                    json.dumps(match_breakdown),
                    sharepoint_link,
                    source,
                    json.dumps(result, ensure_ascii=False),
                    existing["id"],
                ),
            )
            return existing["id"]
        else:
            candidate_id = _generate_atomic_candidate_id(cur, job_id)
            cur.execute(
                """
                INSERT INTO candidates (
                    candidate_id, job_id, full_name, email, phone, location,
                    current_title, current_company, total_experience, match_score,
                    match_breakdown, resume_filename, sharepoint_link, raw_json, source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                RETURNING id
            """,
                (
                    candidate_id,
                    job_id,
                    personal.get("full_name", "Unknown"),
                    personal.get("email", ""),
                    personal.get("phone", ""),
                    personal.get("location", ""),
                    employment.get("current_job_title", ""),
                    employment.get("current_organization", ""),
                    career.get("total_experience_in_years", 0.0),
                    match.get("overall_match_score", 0),
                    json.dumps(match_breakdown),
                    resume_filename,
                    sharepoint_link,
                    json.dumps(result, ensure_ascii=False),
                    source,
                ),
            )
            return cur.fetchone()["id"]


def get_candidates_for_role(role_name: str) -> list:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT c.*, j.role_name 
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE j.role_name = %s
            ORDER BY c.match_score DESC, c.screened_at DESC
        """,
            (role_name,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_unsynced_candidates() -> List[Dict]:
    with get_cursor() as cur:
        cur.execute("""
            SELECT c.id, c.email, c.full_name, c.job_id, j.role_name 
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE c.form_responses IS NULL
        """)
        return [dict(r) for r in cur.fetchall()]


def get_all_candidates(min_score: int = 0) -> list:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT c.*, j.role_name 
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE c.match_score >= %s
            ORDER BY c.match_score DESC, c.screened_at DESC
        """,
            (min_score,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_candidate_by_id(cid: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT c.*, j.role_name 
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE c.id = %s
        """,
            (cid,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_candidate_by_visible_id(candidate_id: str) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT c.*, j.role_name 
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE c.candidate_id = %s
        """,
            (candidate_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_candidates_by_ids(ids: list) -> list:
    if not ids:
        return []
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT c.*, j.role_name 
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE c.id IN %s
        """,
            (tuple(ids),),
        )
        return [dict(r) for r in cur.fetchall()]


def mark_outreach_sent(candidate_id: int, meeting_link: str = "") -> None:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET outreach_sent = 1, outreach_sent_at = NOW(), meeting_link = %s WHERE id = %s",
            (meeting_link, candidate_id),
        )


def update_candidate_form_response(email: str, response_json: dict) -> bool:
    if not isinstance(response_json, dict):
        return False
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET form_responses = %s::jsonb WHERE LOWER(email) = LOWER(%s)",
            (Json(response_json), email),
        )
        return cur.rowcount > 0


def update_candidate_form_score(candidate_id: int, score: int) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET form_score = %s WHERE id = %s", (score, candidate_id)
        )
        return cur.rowcount > 0


def update_candidate_selection_status(candidate_id: int, status: str) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET selection_status = %s WHERE id = %s",
            (status, candidate_id),
        )
        return cur.rowcount > 0


def bulk_update_candidate_status(candidate_ids: list, status: str) -> int:
    if not candidate_ids:
        return 0
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET selection_status = %s WHERE id IN %s",
            (status, tuple(candidate_ids)),
        )
        return cur.rowcount


def get_stats() -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM candidates")
        total = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM candidates WHERE outreach_sent = 1")
        sent = cur.fetchone()["cnt"]
        cur.execute("SELECT AVG(match_score) AS avg FROM candidates")
        avg_sc = cur.fetchone()["avg"] or 0
        return {
            "total_screened": total,
            "outreach_sent": sent,
            "pending_outreach": total - sent,
            "avg_score": round(float(avg_sc), 1),
        }


def update_candidate_qa_score(candidate_id: str, qa_score: int) -> bool:
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET qa_score = %s WHERE candidate_id = %s",
            (qa_score, candidate_id),
        )
        return cur.rowcount > 0


def update_candidate_match_score(candidate_db_id: int, match_score: int) -> bool:
    """Update the match_score for a candidate by its database primary key (id)."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET match_score = %s WHERE id = %s",
            (match_score, candidate_db_id),
        )
        return cur.rowcount > 0


def delete_candidates_by_ids(candidate_ids: list[int]) -> tuple[int, list[dict]]:
    """
    Hard-delete candidates by their integer PKs.
    Returns (deleted_count, list of {id, resume_filename, role_name}) so the
    caller can clean up SharePoint files if requested.
    ON DELETE CASCADE in the schema automatically removes related
    call_qa_results rows.
    """
    if not candidate_ids:
        return 0, []

    with get_cursor(commit=True) as cur:
        # Fetch filenames BEFORE deletion so caller can remove SP files
        cur.execute(
            """
            SELECT c.id, c.resume_filename, j.role_name
            FROM candidates c
            LEFT JOIN jobs j ON j.id = c.job_id
            WHERE c.id = ANY(%s)
            """,
            (candidate_ids,),
        )
        rows = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "DELETE FROM candidates WHERE id = ANY(%s)",
            (candidate_ids,),
        )
        deleted = cur.rowcount

    return deleted, rows
