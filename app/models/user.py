# app/models/user.py — Flask-Login UserMixin implementation for session management.

from flask_login import UserMixin


class User(UserMixin):
    def __init__(self, user_id: int, username: str, is_admin: bool):
        self.id = user_id
        self.username = username
        self.is_admin = bool(is_admin)
