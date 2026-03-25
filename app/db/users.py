# app/db/users.py — User management and role-based access control.

import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
from app.db.connection import get_cursor
from app.utils.role_access import VALID_ROLES


def create_user(
    username: str,
    password: str,
    is_admin: int = 0,
    role: str = "recruiter",
) -> tuple[bool, str]:
    """
    Create a new user with a hashed password and an explicit role.

    *role* must be one of VALID_ROLES: admin / hr / recruiter / interviewer.
    If *is_admin* is 1 the role is forced to 'admin' regardless of the *role* arg.
    """
    if is_admin:
        role = "admin"

    if role not in VALID_ROLES:
        return (
            False,
            f"Invalid role '{role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}.",
        )

    hash_pw = generate_password_hash(password)
    try:
        with get_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, is_admin, role)
                VALUES (%s, %s, %s, %s)
                """,
                (username, hash_pw, is_admin, role),
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


def update_user_role(user_id: int, role: str) -> tuple[bool, str]:
    """Update a user's role. Only admins should call this endpoint."""
    if role not in VALID_ROLES:
        return False, f"Invalid role '{role}'."
    try:
        with get_cursor(commit=True) as cur:
            # Keep is_admin in sync for backward compatibility
            is_admin = 1 if role == "admin" else 0
            cur.execute(
                "UPDATE users SET role = %s, is_admin = %s WHERE id = %s",
                (role, is_admin, user_id),
            )
            if cur.rowcount == 0:
                return False, "User not found."
        return True, f"Role updated to '{role}'."
    except Exception as e:
        return False, str(e)


def get_all_users() -> list[dict]:
    """Fetch all users (admin-only endpoint)."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, username, is_admin, role, created_at FROM users ORDER BY id"
        )
        return [dict(r) for r in cur.fetchall()]
