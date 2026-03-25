# app/routes/api_users.py — Admin-only user management API.
# Only users with role='admin' can access these endpoints.

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from app.db.users import get_all_users, update_user_role, create_user
from app.utils.role_access import require_role, VALID_ROLES, display_role

api_users_bp = Blueprint("api_users", __name__)


@api_users_bp.route("/api/admin/users", methods=["GET"])
@login_required
@require_role("admin")
def list_users():
    """Return all users with their roles. Admin only."""
    try:
        users = get_all_users()
        for u in users:
            u["display_role"] = display_role(u.get("role", "recruiter"))
            if u.get("created_at"):
                u["created_at"] = u["created_at"].isoformat()
        return jsonify({"success": True, "users": users})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_users_bp.route("/api/admin/users/<int:user_id>/role", methods=["PATCH"])
@login_required
@require_role("admin")
def change_user_role(user_id: int):
    """
    Update a user's role.
    Body: { "role": "hr" }
    Admin only — admins cannot demote themselves (safety guard).
    """
    if user_id == current_user.id:
        return jsonify({"error": "You cannot change your own role."}), 400

    data = request.get_json(silent=True) or {}
    new_role = (data.get("role") or "").strip().lower()

    if new_role not in VALID_ROLES:
        return jsonify(
            {
                "error": f"Invalid role '{new_role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}."
            }
        ), 400

    success, msg = update_user_role(user_id, new_role)
    if success:
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400


@api_users_bp.route("/api/admin/users", methods=["POST"])
@login_required
@require_role("admin")
def create_new_user():
    """
    Create a new user with an explicit role.
    Body: { "username": "jdoe", "password": "...", "role": "hr" }
    Admin only.
    """
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    role = (data.get("role") or "recruiter").strip().lower()

    if not username or not password:
        return jsonify({"error": "username and password are required."}), 400

    if role not in VALID_ROLES:
        return jsonify({"error": f"Invalid role '{role}'."}), 400

    is_admin = 1 if role == "admin" else 0
    success, msg = create_user(username, password, is_admin=is_admin, role=role)
    if success:
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400


@api_users_bp.route("/api/admin/roles", methods=["GET"])
@login_required
@require_role("admin")
def list_roles():
    """Return all valid roles with their display names. Admin only."""
    return jsonify(
        {
            "success": True,
            "roles": [
                {"value": r, "label": display_role(r)} for r in sorted(VALID_ROLES)
            ],
        }
    )
