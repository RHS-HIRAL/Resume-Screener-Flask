# app/routes/api_sharepoint.py — API endpoints for fetching SharePoint files and matching folders.

import threading
from flask import Blueprint, request, jsonify
from flask_login import login_required

from app.services.sharepoint import SharePointMatchScoreUpdater
from app.services.sharepoint_sync import run_sync, get_last_sync_time
from app.utils.helpers import normalize_slug

api_sharepoint_bp = Blueprint("api_sharepoint", __name__)

# Track if a sync is currently in progress
_sync_running = False
_sync_lock = threading.Lock()


@api_sharepoint_bp.route("/api/sp/sync", methods=["POST"])
@login_required
def api_sp_sync():
    """Trigger the full Outlook→SharePoint sync + JD scrape pipeline."""
    global _sync_running
    with _sync_lock:
        if _sync_running:
            return jsonify({"success": False, "error": "Sync already in progress"}), 409
        _sync_running = True

    def _run():
        global _sync_running
        try:
            run_sync()
        except Exception:
            pass
        finally:
            with _sync_lock:
                _sync_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Sync started in background"})


@api_sharepoint_bp.route("/api/sp/last-sync", methods=["GET"])
@login_required
def api_sp_last_sync():
    """Return the timestamp of the last successful sync."""
    ts = get_last_sync_time()
    return jsonify({"last_sync": ts})


@api_sharepoint_bp.route("/api/sp/files")
@login_required
def api_sp_files():
    """Return grouped resume folders and flat JD list from SharePoint."""
    try:
        sp = SharePointMatchScoreUpdater()
        resumes = sp.list_resumes_grouped()
        jds = sp.list_jd_files()
        return jsonify({"resumes": resumes, "jds": jds, "connected": True})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 500


@api_sharepoint_bp.route("/api/sp/content")
@login_required
def api_sp_content():
    """Download the text content of a single SharePoint item by its Graph API item_id."""
    item_id = request.args.get("item_id")
    if not item_id:
        return jsonify({"error": "No item_id provided"}), 400

    try:
        sp = SharePointMatchScoreUpdater()
        content = sp.download_text_content(item_id)
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_sharepoint_bp.route("/api/sp/match-folder", methods=["POST"])
@login_required
def api_sp_match_folder():
    """
    Given a JD filename, find the matching resume folder in SharePoint.
    Returns the folder name and the list of resume files inside it.
    """
    data = request.json or {}
    jd_name = data.get("jd_name", "")
    if not jd_name:
        return jsonify({"error": "Missing jd_name"}), 400

    jd_slug = normalize_slug(jd_name)
    print(f"[BULK] JD slug: '{jd_slug}' (from '{jd_name}')")

    try:
        sp = SharePointMatchScoreUpdater()
        resumes_grouped = sp.list_resumes_grouped()

        matched_folder = None
        matched_files = []

        for folder_name, files in resumes_grouped.items():
            folder_slug = normalize_slug(folder_name)
            if folder_slug == jd_slug:
                matched_folder = folder_name
                matched_files = files
                break

        if not matched_folder:
            return jsonify(
                {
                    "matched": False,
                    "jd_slug": jd_slug,
                    "available_folders": list(resumes_grouped.keys()),
                }
            )

        return jsonify(
            {
                "matched": True,
                "folder_name": matched_folder,
                "resume_count": len(matched_files),
                "resumes": matched_files,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
