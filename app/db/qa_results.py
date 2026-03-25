# app/db/qa_results.py — Call QA pipeline storage and retrieval.

from psycopg2.extras import Json
from app.db.connection import get_cursor


def save_qa_result(
    candidate_fk: int,
    audio_filename: str,
    stt_job_id: str,
    conversation_file: str,
    conversation_text: str,
    score_text: str,
    eval_file: str,
    token_meta: dict,
) -> int:
    """Persist or update a QA pipeline result linked to the integer candidate_fk."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM call_qa_results WHERE candidate_fk = %s LIMIT 1",
            (candidate_fk,),
        )
        existing = cur.fetchone()

        if existing:
            cur.execute(
                """
                UPDATE call_qa_results SET
                    audio_filename = %s, stt_job_id = %s, conversation_file = %s,
                    conversation_text = %s, score_text = %s, eval_file = %s,
                    token_meta = %s, created_at = NOW()
                WHERE id = %s RETURNING id
                """,
                (
                    audio_filename,
                    stt_job_id,
                    conversation_file,
                    conversation_text,
                    score_text,
                    eval_file,
                    Json(token_meta),
                    existing["id"],
                ),
            )
            return cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO call_qa_results (
                    candidate_fk, audio_filename, stt_job_id,
                    conversation_file, conversation_text,
                    score_text, eval_file, token_meta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    candidate_fk,
                    audio_filename,
                    stt_job_id,
                    conversation_file,
                    conversation_text,
                    score_text,
                    eval_file,
                    Json(token_meta),
                ),
            )
            return cur.fetchone()["id"]


def get_qa_results_by_candidate_fk(candidate_fk: int) -> list[dict]:
    """Fetch all QA evaluations for a candidate, ordered newest first."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, candidate_fk, audio_filename, stt_job_id, conversation_text,
                   score_text, eval_file, token_meta, created_at
            FROM call_qa_results
            WHERE candidate_fk = %s
            ORDER BY created_at DESC
            """,
            (candidate_fk,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_all_evaluated_candidates() -> list[dict]:
    """
    Return all candidates who have a qa_score, joined with job info
    and latest call_qa_results metadata. Ordered by qa_score DESC.
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT c.id, c.candidate_id, c.full_name, c.email, c.phone,
                   c.match_score, c.qa_score, c.call_selection_status, c.selection_status,
                   c.match_breakdown, c.resume_filename,
                   j.role_name,
                   cq.created_at AS eval_date,
                   cq.token_meta
            FROM candidates c
            JOIN jobs j ON c.job_id = j.id
            LEFT JOIN LATERAL (
                SELECT created_at, token_meta
                FROM call_qa_results
                WHERE candidate_fk = c.id
                ORDER BY created_at DESC
                LIMIT 1
            ) cq ON true
            WHERE c.qa_score IS NOT NULL
            ORDER BY c.qa_score DESC, c.full_name ASC
        """)
        return [dict(r) for r in cur.fetchall()]


def get_latest_evaluation_for_candidate(candidate_fk: int) -> dict | None:
    """
    Return the most recent call_qa_results row for a candidate,
    including full score_text, conversation_text, and token_meta.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, candidate_fk, audio_filename, conversation_text,
                   score_text, token_meta, call_eval_decision, created_at
            FROM call_qa_results
            WHERE candidate_fk = %s
            ORDER BY created_at DESC
            LIMIT 1
        """,
            (candidate_fk,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_call_eval_decision(candidate_fk: int, decision: str) -> bool:
    """Update the call_eval_decision on the latest QA result for audit."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE call_qa_results
            SET call_eval_decision = %s
            WHERE id = (
                SELECT id FROM call_qa_results
                WHERE candidate_fk = %s
                ORDER BY created_at DESC
                LIMIT 1
            )
        """,
            (decision, candidate_fk),
        )
        return cur.rowcount > 0
