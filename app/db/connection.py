# app/db/connection.py — Database connection and schema initialization.
import os
from contextlib import contextmanager
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from dotenv import load_dotenv

load_dotenv()

# Initialize a global thread-safe connection pool
try:
    db_pool = ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5433")),
        dbname=os.getenv("PG_DATABASE", "resume_screener"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASSWORD", ""),
    )
except Exception as e:
    print(f"[DB ERROR] Failed to initialize connection pool: {e}")
    db_pool = None


@contextmanager
def get_cursor(commit=False):
    """Checkout a connection from the pool, yield a cursor, and safely return it."""
    if db_pool is None:
        raise RuntimeError(
            "[DB ERROR] Connection pool is not initialized. Check your database credentials in .env."
        )
    con = db_pool.getconn()
    try:
        cur = con.cursor(cursor_factory=RealDictCursor)
        yield cur
        if commit:
            con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        cur.close()
        db_pool.putconn(con)


def init_db() -> None:
    """Create optimized, normalized tables and indexes."""
    with get_cursor(commit=True) as cur:
        # 1. Users Table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        # ── RBAC migration: add role column if it doesn't exist ──────────────
        cur.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'recruiter';"
        )

        # Back-fill role for any existing users based on is_admin flag.
        # is_admin=1 → admin, is_admin=0 and role still default → recruiter (no change needed).
        cur.execute("""
            UPDATE users
            SET role = 'admin'
            WHERE is_admin = 1 AND (role IS NULL OR role = 'recruiter');
        """)

        # 2. Jobs Table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            jd_filename TEXT NOT NULL,
            role_name TEXT NOT NULL,
            jd_text TEXT,
            form_excel_name TEXT,
            scoring_weights JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        # 2b. WordPress integration columns (safe migration)
        cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS wp_job_id INTEGER;")
        cur.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS date_posted TEXT;")

        # 3. Atomic Sequence Table (Prevents candidate_id race conditions)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS job_sequences (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            next_seq INTEGER DEFAULT 1
        );
        """)

        # 4. Candidates Table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id SERIAL PRIMARY KEY,
            candidate_id TEXT UNIQUE NOT NULL,
            job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            location TEXT,
            current_title TEXT,
            current_company TEXT,
            total_experience REAL,
            match_score INTEGER,
            match_breakdown JSONB,
            resume_filename TEXT,
            sharepoint_link TEXT,
            outreach_sent INTEGER DEFAULT 0,
            outreach_sent_at TIMESTAMP,
            meeting_link TEXT,
            screened_at TIMESTAMP DEFAULT NOW(),
            raw_json TEXT,
            form_responses JSONB,
            selection_status TEXT DEFAULT 'Pending',
            form_score INTEGER DEFAULT NULL,
            qa_score INTEGER DEFAULT NULL,
            rescore_feedback TEXT DEFAULT NULL,
            source TEXT
            );
        """)

        # 4b. rescore_feedback
        cur.execute("""
        ALTER TABLE candidates ADD COLUMN IF NOT EXISTS rescore_feedback TEXT DEFAULT NULL;
        """)

        hr_columns = [
            "ta_spoc TEXT",
            "native_location TEXT",
            "offer_in_hand TEXT",  # encrypted
            "shift_flexibility TEXT",
            "reason_for_change TEXT",
            "ta_hr_comments TEXT",  # encrypted
            "offer_details TEXT",
            "doj TEXT",
            "name_of_source TEXT",
            "current_ctc TEXT",  # encrypted
            "expected_ctc TEXT",  # encrypted
        ]
        for col_def in hr_columns:
            cur.execute(f"ALTER TABLE candidates ADD COLUMN IF NOT EXISTS {col_def};")

        # 4d. Call evaluation columns
        cur.execute(
            "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS next_round TEXT DEFAULT NULL;"
        )
        cur.execute(
            "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS call_selection_status TEXT DEFAULT NULL;"
        )

        # 5. Call QA Results
        cur.execute("""
        CREATE TABLE IF NOT EXISTS call_qa_results (
            id SERIAL PRIMARY KEY,
            candidate_fk INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            audio_filename TEXT,
            stt_job_id TEXT,
            conversation_file TEXT,
            conversation_text TEXT,
            score_text TEXT,
            eval_file TEXT,
            token_meta JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        cur.execute(
            "ALTER TABLE call_qa_results ADD COLUMN IF NOT EXISTS call_eval_decision TEXT DEFAULT NULL;"
        )

        # 6. Strategic Indexing
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_job_id ON candidates(job_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_email ON candidates(email);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(selection_status);"
        )
