# app/db/__init__.py
# Data Access Layer — exposes init_db for the application entry point.

from app.db.connection import init_db, get_cursor

__all__ = ["init_db", "get_cursor"]
