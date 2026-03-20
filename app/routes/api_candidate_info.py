# app/routes/api_candidate_info.py
# API endpoints for the Candidate Information page.

from flask import Blueprint, request, jsonify
from flask_login import login_required

from app.db.candidates import (
    get_roles_with_selected_candidates,
    get_selected_candidates_for_role,
    get_candidate_full_profile,
    update_candidate_hr_fields,
)

api_candidate_info_bp = Blueprint("api_candidate_info", __name__)


@api_candidate_info_bp.route("/api/candidate-info/roles")
@login_required
def api_ci_roles():
    """Return all roles that have at least one Selected candidate."""
    try:
        roles = get_roles_with_selected_candidates()
        return jsonify({"success": True, "roles": roles})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_candidate_info_bp.route("/api/candidate-info/by-role/<int:job_id>")
@login_required
def api_ci_candidates_for_role(job_id: int):
    """Return Selected candidates for a given job_id."""
    try:
        candidates = get_selected_candidates_for_role(job_id)
        return jsonify({"success": True, "candidates": candidates})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_candidate_info_bp.route("/api/candidate-info/<int:candidate_id>")
@login_required
def api_ci_profile(candidate_id: int):
    """Return the full enriched profile for a single candidate."""
    try:
        profile = get_candidate_full_profile(candidate_id)
        if not profile:
            return jsonify({"success": False, "error": "Candidate not found"}), 404
        return jsonify({"success": True, "profile": profile})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_candidate_info_bp.route("/api/candidate-info/<int:candidate_id>/hr", methods=["POST"])
@login_required
def api_ci_save_hr(candidate_id: int):
    """Save the 9 HR-editable fields for a candidate."""
    data = request.get_json(silent=True) or {}
    try:
        updated = update_candidate_hr_fields(candidate_id, data)
        if not updated:
            return jsonify({"success": False, "error": "Candidate not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500