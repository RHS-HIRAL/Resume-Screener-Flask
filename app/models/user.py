# app/models/user.py — Flask-Login UserMixin implementation for session management.

from flask_login import UserMixin


class User(UserMixin):
    def __init__(
        self, user_id: int, username: str, is_admin: bool, role: str = "recruiter"
    ):
        self.id = user_id
        self.username = username
        self.is_admin = bool(is_admin)
        # role is the authoritative permission level going forward.
        # Fallback: if is_admin and role is somehow missing, treat as admin.
        self.role = role if role else ("admin" if is_admin else "recruiter")

    @property
    def display_role(self) -> str:
        from app.utils.role_access import display_role

        return display_role(self.role)
