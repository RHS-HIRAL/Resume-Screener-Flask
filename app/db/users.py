import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
from app.db.connection import get_cursor


def create_user(username: str, password: str, is_admin: int = 0) -> tuple[bool, str]:
    """Create a new user with a hashed password."""
    hash_pw = generate_password_hash(password)
    try:
        with get_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (%s, %s, %s)",
                (username, hash_pw, is_admin),
            )
        return True, "User created successfully."
    except psycopg2.errors.UniqueViolation:
        return False, "Username already exists."
    except Exception as e:
        return False, str(e)


def get_user_by_username(username: str) -> dict | None:
    """Fetch a user by username."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    """Fetch a user by their integer ID (used by Flask-Login)."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def verify_user(username: str, password: str) -> dict | None:
    """Verify credentials and return the user dictionary if successful."""
    user = get_user_by_username(username)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None
