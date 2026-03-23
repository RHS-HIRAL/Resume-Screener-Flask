from psycopg2.extras import Json
from app.db.connection import get_cursor


def upsert_job(
    job_id: int,
    jd_filename: str,
    role_name: str,
    jd_text: str,
    wp_job_id: int = None,
    date_posted: str = None,
) -> int:
    """Insert a job or update its JD content if it already exists."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO jobs (id, jd_filename, role_name, jd_text, wp_job_id, date_posted)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                jd_filename = EXCLUDED.jd_filename,
                role_name   = EXCLUDED.role_name,
                jd_text     = EXCLUDED.jd_text,
                wp_job_id   = COALESCE(EXCLUDED.wp_job_id, jobs.wp_job_id),
                date_posted = COALESCE(EXCLUDED.date_posted, jobs.date_posted)
            RETURNING id
            """,
            (job_id, jd_filename, role_name, jd_text, wp_job_id, date_posted),
        )
        return cur.fetchone()["id"]


def get_all_jobs() -> list[dict]:
    """Fetch all jobs, ordered by newest first."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, jd_filename, role_name, jd_text, form_excel_name,
                   scoring_weights, wp_job_id, date_posted, created_at
            FROM jobs ORDER BY id DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]


def get_all_unique_job_forms() -> list[str]:
    """Return a list of all unique Excel filenames mapped to active jobs."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT form_excel_name FROM jobs WHERE form_excel_name IS NOT NULL AND form_excel_name != ''"
        )
        return [row["form_excel_name"] for row in cur.fetchall()]


def update_job_form_excel(job_id: int, excel_name: str) -> bool:
    """Map a specific MS Form Excel sheet to a job."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE jobs SET form_excel_name = %s WHERE id = %s", (excel_name, job_id)
        )
        return cur.rowcount > 0


def update_job_scoring_weights(job_id: int, weights: dict) -> bool:
    """Update custom scoring weights for a role."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE jobs SET scoring_weights = %s WHERE id = %s",
            (Json(weights), job_id),
        )
        return cur.rowcount > 0


def get_jd_text(job_id: int) -> str:
    """Fetch the raw JD text for a job."""
    with get_cursor() as cur:
        cur.execute("SELECT jd_text FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        return row["jd_text"] if row else ""
