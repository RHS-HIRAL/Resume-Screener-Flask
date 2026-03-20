# app/routes/api_upload.py — API endpoints for manual resume upload with required source validation

import os
import shutil
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Blueprint, request, jsonify
from flask_login import login_required

from config import Config
from app.services.sharepoint import SharePointMatchScoreUpdater
from app.services.resume_sync import extract_raw_text

api_upload_bp = Blueprint("api_upload", __name__)


@api_upload_bp.route("/api/upload/subfolders", methods=["GET"])
@login_required
def get_subfolders():
    """Fetch all subfolders inside the configured SHAREPOINT_JOBS_FOLDER."""
    try:
        sp = SharePointMatchScoreUpdater()
        jobs_folder = Config.SHAREPOINT_JOBS_FOLDER.strip("/")

        # Use existing protected method to list children of a folder
        items = sp._list_folder_children(jobs_folder)

        # Filter for folders only
        folders = [
            {"id": item["id"], "name": item["name"]}
            for item in items
            if "folder" in item
        ]

        return jsonify({"success": True, "folders": folders})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_upload_bp.route("/api/upload/manual", methods=["POST"])
@login_required
def manual_upload():
    """
    Handle manual upload of multiple resumes with REQUIRED source selection.
    For each resume:
      - convert to TXT
      - if success: upload PDF/Docx + TXT to SHAREPOINT_JOBS_FOLDER/<subfolder>
      - if fail: upload PDF/Docx to SHAREPOINT_JOBS_FOLDER/<subfolder>/corrupted folder
      - add Source metadata to the PDF/Docx only in SharePoint
    """
    if "resumes" not in request.files:
        return jsonify({"success": False, "error": "No files part"}), 400

    target_folder = request.form.get("target_folder")
    if not target_folder:
        return jsonify({"success": False, "error": "Target subfolder is required"}), 400

    files = request.files.getlist("resumes")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"success": False, "error": "No selected files"}), 400

    # BACKEND VALIDATION: Ensure all files have a source
    missing_sources = []
    for file in files:
        if file.filename == "":
            continue
        source_value = request.form.get(f"source_{file.filename}", "").strip()
        if not source_value:
            missing_sources.append(file.filename)

    if missing_sources:
        return jsonify(
            {
                "success": False,
                "error": f"Source is required for all resumes. Missing source for: {', '.join(missing_sources[:3])}{'...' if len(missing_sources) > 3 else ''}",
            }
        ), 400

    sp = SharePointMatchScoreUpdater()
    jobs_base = Config.SHAREPOINT_JOBS_FOLDER.strip("/")
    sp_dest_folder = f"{jobs_base}/{target_folder}"

    # Extract job_id from target_folder (e.g., '9456' from '9456_Jira_Developer')
    job_id = target_folder.split("_")[0] if "_" in target_folder else "unknown"

    # Corrupted folder is in the main jobs folder, not inside the subfolder
    sp_corrupted_folder = f"{jobs_base}/corrupted folder"

    # Temporary local directory for processing
    tmp_dir = Path(Config.SYNC_TEMP_RESUMES_DIR) / "manual_upload"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    corrupted_count = 0

    try:
        for file in files:
            if file.filename == "":
                continue

            orig_stem = Path(file.filename).stem
            orig_ext = Path(file.filename).suffix

            # Renaming logic: original_name_job_id.ext
            renamed_basename = f"{orig_stem}_{job_id}"
            renamed_filename = f"{renamed_basename}{orig_ext}"

            # Get and validate source (already validated above, but double-check)
            source_value = request.form.get(f"source_{file.filename}", "").strip()
            if not source_value:
                print(f"[ManualUpload] Skipping {file.filename} - no source provided")
                continue

            local_file_path = tmp_dir / renamed_filename
            file.save(local_file_path)

            try:
                # 1. Convert to Text
                text_content = extract_raw_text(local_file_path)

                with open(local_file_path, "rb") as f:
                    file_bytes = f.read()

                # Check if conversion failed (empty or too short)
                is_corrupted = not text_content or len(text_content.strip()) < 10

                if is_corrupted:
                    # 2b. Corrupted: Upload renamed original to corrupted folder in JOBS root
                    sp_item_id = sp.upload_file(
                        sp_corrupted_folder, renamed_filename, file_bytes
                    )

                    # 3b. Push Metadata to corrupted file (including required Source)
                    sp.push_metadata(
                        filename=renamed_filename,
                        metadata={"Source": source_value},
                        confirmed_item_id=sp_item_id,
                        overwrite=True,
                    )
                    corrupted_count += 1
                else:
                    # 2a. Success: Upload renamed original to destination folder
                    sp_item_id = sp.upload_file(
                        sp_dest_folder, renamed_filename, file_bytes
                    )

                    # Upload TXT file to destination folder (also renamed)
                    txt_filename = f"{renamed_basename}.txt"
                    sp.upload_file(sp_dest_folder, txt_filename, text_content)

                    # 3a. Push Metadata only to the renamed original PDF/Docx (including required Source)
                    sp.push_metadata(
                        filename=renamed_filename,
                        metadata={"Source": source_value},
                        confirmed_item_id=sp_item_id,
                        overwrite=True,
                    )
                    success_count += 1

            except Exception as e:
                print(f"[ManualUpload] Error processing {renamed_filename}: {e}")
                # We do not count it as success or corrupted if it completely throws an exception
                pass
            finally:
                # Clean up local file
                local_file_path.unlink(missing_ok=True)

    finally:
        # Clean up tmp directory
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify(
        {
            "success": True,
            "success_count": success_count,
            "corrupted_count": corrupted_count,
        }
    )
