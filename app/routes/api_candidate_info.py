# app/routes/api_candidate_info.py
# API endpoints for the Candidate Information page.

from flask import Blueprint, request, jsonify, Response, stream_with_context
from flask_login import login_required, current_user

from app.db.candidates import (
    get_roles_with_candidates,
    get_screened_candidates_for_role,
    get_candidate_full_profile,
    update_candidate_full_profile,
)

api_candidate_info_bp = Blueprint("api_candidate_info", __name__)


@api_candidate_info_bp.route("/api/candidate-info/roles")
@login_required
def api_ci_roles():
    """Return all roles that have at least one screened candidate."""
    try:
        roles = get_roles_with_candidates()
        return jsonify({"success": True, "roles": roles})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_candidate_info_bp.route("/api/candidate-info/by-role/<int:job_id>")
@login_required
def api_ci_candidates_for_role(job_id: int):
    """Return all screened candidates for a given job_id."""
    try:
        candidates = get_screened_candidates_for_role(job_id)
        return jsonify({"success": True, "candidates": candidates})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_candidate_info_bp.route("/api/candidate-info/<int:candidate_id>")
@login_required
def api_ci_profile(candidate_id: int):
    """Return the full enriched profile for a single candidate."""
    try:
        user_role = getattr(current_user, "role", "recruiter")
        profile = get_candidate_full_profile(candidate_id, user_role=user_role)
        if not profile:
            return jsonify({"success": False, "error": "Candidate not found"}), 404
        return jsonify({"success": True, "profile": profile})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_candidate_info_bp.route(
    "/api/candidate-info/<int:candidate_id>/hr", methods=["POST"]
)
@login_required
def api_ci_save_hr(candidate_id: int):
    """Save the fully editable profile details for a candidate."""
    data = request.get_json(silent=True) or {}
    try:
        user_role = getattr(current_user, "role", "recruiter")
        updated = update_candidate_full_profile(candidate_id, data, user_role=user_role)
        if not updated:
            return jsonify({"success": False, "error": "Candidate not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_candidate_info_bp.route("/api/candidate-info/<int:candidate_id>/resume")
@login_required
def api_ci_resume(candidate_id: int):
    """
    Proxy the candidate's resume PDF from SharePoint.
    Lookup strategy:
      1. Get job_id and resume_filename from the candidate profile.
      2. List subfolders under SHAREPOINT_JOBS_FOLDER and find the one
         whose name starts with '{job_id}_'.
      3. List files in that subfolder and find the one whose name
         matches resume_filename (case-insensitive).
      4. Download and stream the bytes inline so the browser can render it.
    """
    import logging
    log = logging.getLogger(__name__)
    log.info(f"[RESUME] ── Step 0: Request received for candidate_id={candidate_id}")

    try:
        profile = get_candidate_full_profile(candidate_id)
        if not profile:
            log.warning(f"[RESUME] ✗ Step 1 FAILED: No profile found for candidate_id={candidate_id}")
            return jsonify({"success": False, "error": "Candidate not found"}), 404
        log.info(f"[RESUME] ✓ Step 1: Profile loaded — full_name={profile.get('full_name')}")

        job_id = profile.get("job_id")
        resume_filename = profile.get("resume_filename", "").strip()

        log.info(f"[RESUME]   job_id={job_id}, resume_filename='{resume_filename}'")

        if not job_id or not resume_filename:
            log.warning(f"[RESUME] ✗ Step 2 FAILED: Missing resume info — job_id={job_id}, resume_filename='{resume_filename}'")
            return jsonify({"success": False, "error": "No resume info available"}), 404
        log.info(f"[RESUME] ✓ Step 2: Resume info present")

        from app.services.sharepoint import SharePointMatchScoreUpdater
        from config import Config

        sp = SharePointMatchScoreUpdater()

        # Step 3: Find the subfolder that starts with the job_id
        prefix = str(job_id) + "_"
        log.info(f"[RESUME]   Listing folders in '{Config.SHAREPOINT_JOBS_FOLDER}' with prefix '{prefix}'")
        all_items = sp._list_folder_children(Config.SHAREPOINT_JOBS_FOLDER)
        folder_names = [item["name"] for item in all_items if "folder" in item]
        log.info(f"[RESUME]   Found {len(folder_names)} subfolders: {folder_names[:10]}")
        subfolders = [
            item
            for item in all_items
            if "folder" in item and item["name"].startswith(prefix)
        ]

        if not subfolders:
            log.warning(f"[RESUME] ✗ Step 3 FAILED: No folder starting with '{prefix}' in {Config.SHAREPOINT_JOBS_FOLDER}")
            return jsonify(
                {
                    "success": False,
                    "error": f"No SharePoint folder found for job_id {job_id}",
                }
            ), 404

        target_folder = subfolders[0]["name"]
        folder_path = f"{Config.SHAREPOINT_JOBS_FOLDER}/{target_folder}"
        log.info(f"[RESUME] ✓ Step 3: Matched folder '{target_folder}'")

        # Step 4: Find the file whose name matches resume_filename
        files = sp._list_folder_children(folder_path)
        file_names = [f["name"] for f in files if "file" in f]
        log.info(f"[RESUME]   Found {len(file_names)} files in folder. Looking for '{resume_filename}'")
        log.info(f"[RESUME]   Available files: {file_names[:15]}")
        target_file = next(
            (
                f
                for f in files
                if "file" in f and f["name"].lower() == resume_filename.lower()
            ),
            None,
        )

        if not target_file:
            log.warning(f"[RESUME] ✗ Step 4 FAILED: '{resume_filename}' not found in folder '{target_folder}'. Available: {file_names[:15]}")
            return jsonify(
                {
                    "success": False,
                    "error": f"Resume '{resume_filename}' not found in SharePoint folder '{target_folder}'",
                }
            ), 404
        log.info(f"[RESUME] ✓ Step 4: File matched — '{target_file['name']}' (id={target_file['id']})")

        # Step 3: Download and stream the file
        drive_id = sp._get_drive_id()
        item_id = target_file["id"]
        download_url = f"{sp.GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        headers = sp._get_headers()
        if "Content-Type" in headers:
            del headers["Content-Type"]

        fn_lower = resume_filename.lower()

        # ── DOCX: convert to HTML via mammoth and serve inline ──────────────
        if fn_lower.endswith(".docx"):
            import mammoth, io

            resp = sp.session.get(
                download_url, headers=headers, timeout=60, allow_redirects=True
            )
            resp.raise_for_status()
            result = mammoth.convert_to_html(io.BytesIO(resp.content))
            html_body = result.value

            styled_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{resume_filename}</title>
<style>
  body {{
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 13px;
    line-height: 1.6;
    color: #1a1a2e;
    padding: 2rem 2.5rem;
    max-width: 860px;
    margin: 0 auto;
    background: #fff;
  }}
  h1, h2, h3, h4 {{ color: #0f172a; margin-top: 1.2em; }}
  p {{ margin: 0.4em 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  td, th {{ border: 1px solid #cbd5e1; padding: 0.4em 0.6em; }}
  ul, ol {{ padding-left: 1.4em; }}
  img {{ 
    max-width: 100%; 
    height: auto; 
    display: block; 
    margin: 1em 0; 
    max-height: 150px; 
    object-fit: contain;
  }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
            return Response(styled_html, content_type="text/html; charset=utf-8")

        # ── PDF: stream directly ──────────────
        resp = sp.session.get(
            download_url, headers=headers, timeout=60, stream=True, allow_redirects=True
        )
        resp.raise_for_status()

        if fn_lower.endswith(".pdf"):
            mime = "application/pdf"
            disposition = "inline"
        else:
            mime = "application/octet-stream"
            disposition = "attachment"

        def generate():
            for chunk in resp.iter_content(chunk_size=8192):
                yield chunk

        return Response(
            stream_with_context(generate()),
            content_type=mime,
            headers={
                "Content-Disposition": f'{disposition}; filename="{resume_filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
