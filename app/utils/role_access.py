"""
app/utils/role_access.py
Role definitions, permission constants, and access-gate helpers.

Role hierarchy (highest → lowest privilege):
    admin       — full access, can manage users
    hr          — can read & write sensitive candidate fields (CTC, HR notes)
    recruiter   — standard screening access; sensitive fields masked
    interviewer — read-only; sensitive fields masked
"""

from __future__ import annotations
from functools import wraps
from flask import jsonify
from flask_login import current_user

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_ROLES: frozenset[str] = frozenset({"admin", "hr", "recruiter", "interviewer"})

# Candidate table columns that contain confidential compensation / HR data.
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {"current_ctc", "expected_ctc", "offer_in_hand", "ta_hr_comments"}
)

# ── Per-role capability matrix ────────────────────────────────────────────────

_PERMISSIONS: dict[str, dict[str, bool]] = {
    "admin": {
        "read_sensitive": True,
        "write_sensitive": True,
        "write_candidates": True,
        "manage_users": True,
    },
    "hr": {
        "read_sensitive": True,
        "write_sensitive": True,
        "write_candidates": True,
        "manage_users": False,
    },
    "recruiter": {
        "read_sensitive": False,
        "write_sensitive": False,
        "write_candidates": True,
        "manage_users": False,
    },
    "interviewer": {
        "read_sensitive": False,
        "write_sensitive": False,
        "write_candidates": False,
        "manage_users": False,
    },
}


def _check(role: str, capability: str) -> bool:
    return _PERMISSIONS.get(role, {}).get(capability, False)


# ── Simple boolean helpers (use in route / DB logic) ─────────────────────────


def can_read_sensitive(role: str) -> bool:
    """Return True if *role* is allowed to see plain-text sensitive fields."""
    return _check(role, "read_sensitive")


def can_write_sensitive(role: str) -> bool:
    """Return True if *role* is allowed to update sensitive fields."""
    return _check(role, "write_sensitive")


def can_write_candidates(role: str) -> bool:
    """Return True if *role* is allowed to modify non-sensitive candidate data."""
    return _check(role, "write_candidates")


def can_manage_users(role: str) -> bool:
    """Return True if *role* can create / modify other user accounts."""
    return _check(role, "manage_users")


def is_valid_role(role: str) -> bool:
    return role in VALID_ROLES


def display_role(role: str) -> str:
    """Human-readable role label."""
    return {
        "admin": "Administrator",
        "hr": "HR / TA",
        "recruiter": "Recruiter / Screener",
        "interviewer": "Interviewer",
    }.get(role, role.title())


# ── Flask route decorators ────────────────────────────────────────────────────


def require_role(*allowed_roles: str):
    """
    Decorator that restricts a route to users whose role is in *allowed_roles*.

    Usage::

        @bp.route("/admin/users")
        @login_required
        @require_role("admin")
        def admin_users():
            ...
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            role = getattr(current_user, "role", None)
            if role not in allowed_roles:
                return jsonify(
                    {
                        "error": "You do not have permission to perform this action.",
                        "required_roles": list(allowed_roles),
                        "your_role": role,
                    }
                ), 403
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def require_write_access(fn):
    """Decorator: reject interviewer (read-only) from any write endpoint."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        role = getattr(current_user, "role", "recruiter")
        if not can_write_candidates(role):
            return jsonify(
                {"error": "Your role is read-only and cannot modify candidate data."}
            ), 403
        return fn(*args, **kwargs)

    return wrapper
