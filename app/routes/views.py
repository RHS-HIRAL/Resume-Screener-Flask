# app/routes/views.py — Blueprint for handling all HTML page renders and dashboard views.

import logging
from flask import Blueprint, render_template, flash
from flask_login import login_required

from app.db.candidates import get_stats, get_all_candidates
from app.db.jobs import get_all_jobs

views_bp = Blueprint("views", __name__)


@views_bp.route("/")
@login_required
def dashboard():
    """Main dashboard view with high-level statistics and recent activity."""
    try:
        stats = get_stats()
        all_jobs = get_all_jobs()

        # EFFICIENCY FIX: Ideally, use a dedicated get_recent_candidates(limit=5) in your DB layer.
        # If get_all_candidates supports a limit parameter, use it. Otherwise, we gracefully fallback.
        # Example for candidates.py: SELECT * FROM candidates ORDER BY screened_at DESC LIMIT 5
        all_cands = get_all_candidates()
        recent = all_cands[:5] if all_cands else []

        return render_template(
            "dashboard.html", stats=stats, recent_candidates=recent, all_jobs=all_jobs
        )
    except Exception as e:
        logging.error(f"[VIEWS ERROR] Failed to load dashboard data: {e}")
        flash("Unable to load dashboard data at this time.", "danger")
        return render_template(
            "dashboard.html", stats={}, recent_candidates=[], all_jobs=[]
        )


@views_bp.route("/manual_upload")
@login_required
def manual_upload():
    """UI for Manual Resume Upload."""
    return render_template("manual_upload.html")


@views_bp.route("/screener")
@login_required
def screener():
    """UI for the AI Resume Screener tool."""
    return render_template("screener.html")


@views_bp.route("/outreach")
@login_required
def outreach():
    """UI for bulk email outreach."""
    try:
        roles = get_all_jobs()
        return render_template("outreach.html", roles=roles)
    except Exception as e:
        logging.error(f"[VIEWS ERROR] Failed to load roles for outreach: {e}")
        flash("Unable to load job roles.", "danger")
        return render_template("outreach.html", roles=[])


@views_bp.route("/responses")
@login_required
def responses():
    """UI for reviewing MS Forms responses."""
    try:
        roles = get_all_jobs()
        return render_template("responses.html", roles=roles)
    except Exception as e:
        logging.error(f"[VIEWS ERROR] Failed to load roles for responses: {e}")
        flash("Unable to load job roles.", "danger")
        return render_template("responses.html", roles=[])

@views_bp.route("/candidate-information")
@login_required
def candidate_information():
    return render_template("candidate_information.html")

@views_bp.route("/call-eval-results")
@login_required
def call_eval_results():
    """UI for viewing Call Evaluation Results."""
    return render_template("call_eval_results.html")
