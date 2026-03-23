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
        obj = match.get(key, {})
        return {"status": obj.get("status", ""), "summary": obj.get("summary", "")}

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


def get_breakdown_by_resume(resume_filename: str, job_id: int) -> Optional[dict]:
    """
    Snapshot the current match_breakdown for a candidate BEFORE save_candidate
    overwrites it during a rescore pass. Returns None if no existing record.
    psycopg2 with RealDictCursor deserialises JSONB columns to Python dicts automatically.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT match_breakdown FROM candidates WHERE resume_filename = %s AND job_id = %s LIMIT 1",
            (resume_filename, job_id),
        )
        row = cur.fetchone()
        if row and row["match_breakdown"]:
            bd = row["match_breakdown"]
            return bd if isinstance(bd, dict) else dict(bd)
        return None


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
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET match_score = %s WHERE id = %s",
            (match_score, candidate_db_id),
        )
        return cur.rowcount > 0


def finalize_rescore(
    candidate_db_id: int,
    match_score: int,
    reviewer_feedback: Optional[str] = None,
    old_breakdown: Optional[dict] = None,
    score_choice: str = "new",
) -> bool:
    """
    Persist the final score (and optionally breakdown + feedback) after a rescore.

    score_choice == 'previous':
        • Restore match_score to the previous value (caller passes prev_score as match_score).
        • Restore match_breakdown to old_breakdown, reverting what save_candidate just wrote.
        • Leave rescore_feedback completely unchanged in the DB.

    score_choice in ('new', 'average'):
        • Update match_score to the chosen value.
        • Keep match_breakdown as-is — save_candidate already wrote the correct new one.
        • Persist reviewer_feedback → rescore_feedback column.
    """
    with get_cursor(commit=True) as cur:
        if score_choice == "previous":
            if old_breakdown is not None:
                cur.execute(
                    """
                    UPDATE candidates
                    SET match_score = %s, match_breakdown = %s::jsonb
                    WHERE id = %s
                    """,
                    (match_score, json.dumps(old_breakdown), candidate_db_id),
                )
            else:
                # No old breakdown available — at least revert the score
                cur.execute(
                    "UPDATE candidates SET match_score = %s WHERE id = %s",
                    (match_score, candidate_db_id),
                )
        else:
            # 'new' or 'average': breakdown is already correct; persist feedback
            if reviewer_feedback is not None:
                cur.execute(
                    """
                    UPDATE candidates
                    SET match_score = %s, rescore_feedback = %s
                    WHERE id = %s
                    """,
                    (match_score, reviewer_feedback, candidate_db_id),
                )
            else:
                cur.execute(
                    "UPDATE candidates SET match_score = %s WHERE id = %s",
                    (match_score, candidate_db_id),
                )
        return cur.rowcount > 0


def update_candidate_resume_filename(candidate_db_id: int, new_filename: str) -> bool:
    """
    Update resume_filename in the DB after a SharePoint rename.
    Called from the background SP push thread once the PATCH succeeds.
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET resume_filename = %s WHERE id = %s",
            (new_filename, candidate_db_id),
        )
        return cur.rowcount > 0


def delete_candidates_by_ids(candidate_ids: list[int]) -> tuple[int, list[dict]]:
    """
    Hard-delete candidates by their integer PKs.
    ON DELETE CASCADE removes related call_qa_results rows automatically.
    """
    if not candidate_ids:
        return 0, []

    with get_cursor(commit=True) as cur:
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
import re

def _format_role_display(role_name: str) -> str:
    """Convert '8977_Full_Stack_Development_Intern' → 'Full Stack Development Intern'."""
    name = re.sub(r'^\d+_', '', role_name)
    return name.replace('_', ' ')


def get_roles_with_selected_candidates() -> list:
    with get_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT j.id, j.role_name
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE LOWER(c.selection_status) = 'selected'
            ORDER BY j.role_name
        """)
        rows = cur.fetchall()
    return [
        {"id": r["id"], "role_name": r["role_name"],
         "display_name": _format_role_display(r["role_name"])}
        for r in rows
    ]


def get_selected_candidates_for_role(job_id: int) -> list:
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, candidate_id, full_name
            FROM candidates
            WHERE job_id = %s AND LOWER(selection_status) = 'selected'
            ORDER BY full_name ASC
        """, (job_id,))
        return [dict(r) for r in cur.fetchall()]


def get_candidate_full_profile(candidate_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("""
            SELECT c.*, j.role_name
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            WHERE c.id = %s
        """, (candidate_id,))
        row = cur.fetchone()

    if not row:
        return None

    data = dict(row)

    raw = data.get("raw_json") or "{}"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = {}

    extraction = raw.get("function_2_resume_data_extraction", {})
    career = extraction.get("career_metrics", {})
    edu_history = extraction.get("education_history", [])
    degrees = [e.get("degree", "") for e in edu_history if e.get("degree")]

    form = data.get("form_responses") or {}
    if isinstance(form, str):
        try:
            form = json.loads(form)
        except (json.JSONDecodeError, TypeError):
            form = {}

    return {
        "id": data["id"],
        "job_id": data.get("job_id"),
        "resume_filename": data.get("resume_filename", ""),
        "candidate_id": data.get("candidate_id", ""),
        "position_applied": _format_role_display(data.get("role_name", "")),
        "full_name": data.get("full_name", ""),
        "phone": data.get("phone", ""),
        "email": data.get("email", ""),
        "education": degrees,
        "total_experience": data.get("total_experience"),
        "relative_experience": career.get("relative_years_of_experience"),
        "current_company": data.get("current_company", ""),
        "current_title": data.get("current_title", ""),
        "technical_skills": career.get("technical_skills", []),
        "certifications": career.get("certificates_name", []),
        "current_location": form.get("Current Location", ""),
        "relocation": form.get("Willing to Relocate?", ""),
        "notice_period": form.get("Notice Period", ""),
        "source": data.get("source", ""),
        "ta_spoc": data.get("ta_spoc", "") or "",
        "native_location": data.get("native_location", "") or "",
        "offer_in_hand": data.get("offer_in_hand", "") or "",
        "shift_flexibility": data.get("shift_flexibility", "") or "",
        "reason_for_change": data.get("reason_for_change", "") or "",
        "ta_hr_comments": data.get("ta_hr_comments", "") or "",
        "offer_details": data.get("offer_details", "") or "",
        "doj": data.get("doj", "") or "",
        "name_of_source": data.get("name_of_source", "") or "",
    }


def update_candidate_full_profile(candidate_id: int, data: dict) -> bool:
    with get_cursor(commit=True) as cur:
        # First, fetch current raw_json and form_responses
        cur.execute("SELECT raw_json, form_responses FROM candidates WHERE id = %s", (candidate_id,))
        row = cur.fetchone()
        if not row:
            return False
            
        raw_json_str = row.get("raw_json") or "{}"
        if isinstance(raw_json_str, str):
            try:
                raw_json = json.loads(raw_json_str)
            except (json.JSONDecodeError, TypeError):
                raw_json = {}
        else:
            raw_json = raw_json_str
            
        form_responses_str = row.get("form_responses") or "{}"
        if isinstance(form_responses_str, str):
            try:
                form_responses = json.loads(form_responses_str)
            except (json.JSONDecodeError, TypeError):
                form_responses = {}
        else:
            form_responses = form_responses_str

        # Update raw_json
        if "function_2_resume_data_extraction" not in raw_json:
            raw_json["function_2_resume_data_extraction"] = {}
        extraction = raw_json["function_2_resume_data_extraction"]
        
        if "career_metrics" not in extraction:
            extraction["career_metrics"] = {}
        career = extraction["career_metrics"]

        # Parse skills and certs strings into lists
        skills_str = data.get("technical_skills", "")
        certs_str = data.get("certifications", "")
        
        career["technical_skills"] = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []
        career["certificates_name"] = [c.strip() for c in certs_str.split(",") if c.strip()] if certs_str else []
        
        if data.get("relative_experience") is not None and data.get("relative_experience") != "":
            try:
                career["relative_years_of_experience"] = float(data.get("relative_experience", 0))
            except ValueError:
                pass 
                
        # Parse education string and map back to education_history
        if "education_history" not in extraction:
            extraction["education_history"] = []
        edu_history = extraction["education_history"]
        
        if "education" in data:
            edu_str = data.get("education", "")
            new_degrees = [d.strip() for d in edu_str.split(",") if d.strip()]
            
            # Map new degrees back to existing objects, or create new ones
            for i, degree in enumerate(new_degrees):
                if i < len(edu_history):
                    edu_history[i]["degree"] = degree
                else:
                    edu_history.append({"degree": degree})
            
            # If the user removed degrees, trim the list to match
            if len(new_degrees) < len(edu_history):
                extraction["education_history"] = edu_history[:len(new_degrees)]
            
        # Update form_responses
        form_responses["Current Location"] = data.get("current_location", "")
        form_responses["Willing to Relocate?"] = data.get("relocation", "")
        form_responses["Notice Period"] = data.get("notice_period", "")
        
        # Now update everything
        try:
            total_exp = float(data.get("total_experience")) if data.get("total_experience") else None
        except ValueError:
            total_exp = None
            
        cur.execute("""
            UPDATE candidates SET
                full_name          = %s,
                phone              = %s,
                email              = %s,
                total_experience   = %s,
                current_company    = %s,
                current_title      = %s,
                source             = %s,
                ta_spoc            = %s,
                native_location    = %s,
                offer_in_hand      = %s,
                shift_flexibility  = %s,
                reason_for_change  = %s,
                ta_hr_comments     = %s,
                offer_details      = %s,
                doj                = %s,
                name_of_source     = %s,
                raw_json           = %s,
                form_responses     = %s::jsonb
            WHERE id = %s
        """, (
            data.get("full_name") or None,
            data.get("phone") or None,
            data.get("email") or None,
            total_exp,
            data.get("current_company") or None,
            data.get("current_title") or None,
            data.get("source") or None,
            data.get("ta_spoc") or None,
            data.get("native_location") or None,
            data.get("offer_in_hand") or None,
            data.get("shift_flexibility") or None,
            data.get("reason_for_change") or None,
            data.get("ta_hr_comments") or None,
            data.get("offer_details") or None,
            data.get("doj") or None,
            data.get("name_of_source") or None,
            json.dumps(raw_json, ensure_ascii=False),
            json.dumps(form_responses),
            candidate_id,
        ))
        return cur.rowcount > 0


def update_call_selection_status(candidate_db_id: int, status: str | None) -> bool:
    """
    Update the call_selection_status for a candidate.
    status: 'online_test' | 'technical_round' | 'rejected' | None (resets to Pending)

    IMPORTANT: This does NOT touch selection_status — call-round decisions
    are completely independent from the resume screening status.
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE candidates SET call_selection_status = %s WHERE id = %s",
            (status, candidate_db_id),
        )
        return cur.rowcount > 0


# Keep old name as alias for any existing call sites during transition
def update_candidate_next_round(candidate_db_id: int, decision: str) -> bool:
    """Deprecated: use update_call_selection_status instead."""
    return update_call_selection_status(candidate_db_id, decision)