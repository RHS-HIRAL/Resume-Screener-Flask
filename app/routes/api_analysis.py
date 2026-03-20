# app/routes/api_analysis.py — API endpoints for single and bulk AI resume screening using Gemini.

import os
import re
import json
import time
import threading
import traceback
from flask import Blueprint, request, jsonify, Response, current_app
from flask_login import login_required, current_user

from app.db.jobs import upsert_job
from app.db.candidates import (
    save_candidate,
    update_candidate_match_score,
    update_candidate_resume_filename,
    finalize_rescore,
    get_breakdown_by_resume,
)
from app.services.ai_evaluator import evaluate_resume
from app.services.sharepoint import SharePointMatchScoreUpdater
from app.utils.helpers import extract_job_code

api_analysis_bp = Blueprint("api_analysis", __name__)

# ── Progress Tracking ─────────────────────────────────────────────────────────
# WARNING: In-memory dictionaries do not share state across Gunicorn workers.
# For a multi-worker production deployment, replace this with a Redis hash.
user_progress = {}


def set_progress(user_id: int, percent: int, message: str):
    user_progress[user_id] = {"percent": percent, "message": message}


def get_progress(user_id: int) -> dict:
    return user_progress.get(user_id, {"percent": 0, "message": "Waiting..."})


# ── Helper Functions ──────────────────────────────────────────────────────────


def _clean_candidate_name(full_name: str) -> str:
    """
    Convert a candidate's full name into a filename-safe slug.
    Rules: keep only letters/digits/spaces, replace spaces with underscores.
    e.g. "John A. Smith-Jr" → "John_A_SmithJr"
    """
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", "", full_name)
    return "_".join(cleaned.split())


def _compute_screened_with(jd_filename: str) -> str:
    """
    Derive the 'ScreenedWith' column value from the raw JD filename.
    Stripping the 'JD_' prefix and file extension.
    e.g. "JD_full-stack-development-intern.txt" → "full-stack-development-intern"
    """
    value = re.sub(r"(?i)^JD_", "", jd_filename)
    return os.path.splitext(value)[0]


def _background_sp_push(
    app,
    filename: str,
    metadata: dict,
    role_hint: str,
    item_id: str = "",
    candidate_db_id: int = None,
    job_code: int = None,
):
    """
    Thread target: push metadata to SharePoint then — for non-Website resumes
    whose MatchScore was previously blank — rename the PDF/DOCX and its paired
    .txt file to '<CandidateName>_<job_code>.<ext>' and sync the new name to
    the database.
    """
    with app.app_context():
        try:
            sp = SharePointMatchScoreUpdater()

            # 1. Fetch existing fields BEFORE pushing new metadata so we can
            #    check the original MatchScore and Source values.
            fields = sp.get_item_fields(item_id) if item_id else {}
            source = (fields.get("Source") or "").strip().lower()
            existing_score = fields.get("MatchScore")
            # Consider blank if the field is missing, empty-string, or zero.
            is_blank_score = (
                existing_score is None
                or str(existing_score).strip() == ""
                or existing_score == 0
            )

            # 2. Push metadata (MatchScore, CandidateName, ScreenedWith, …)
            sp.push_metadata(
                filename, metadata, role_hint=role_hint, confirmed_item_id=item_id
            )
            print(f"[SP SYNC] Metadata pushed for {filename}")

            # 3. Rename only when:
            #    a) the score was blank before screening, AND
            #    b) source is an explicit non-Website value
            if (
                item_id
                and candidate_db_id is not None
                and job_code is not None
                and is_blank_score
                and source
                and source != "website"
            ):
                raw_name = (metadata.get("CandidateName") or "").strip()
                if raw_name and raw_name.lower() != "unknown":
                    ext = os.path.splitext(filename)[1]  # .pdf / .docx / etc.
                    clean = _clean_candidate_name(raw_name)
                    new_name = f"{clean}_{job_code}{ext}"

                    # Skip if the file is already correctly named
                    if new_name.lower() == filename.lower():
                        print(f"[SP RENAME] Already named correctly: {filename}")
                    else:
                        # 3a. Rename the primary file (PDF / DOCX)
                        final_name, status = sp.rename_item(item_id, new_name)
                        if status == "OK" and final_name:
                            update_candidate_resume_filename(
                                candidate_db_id, final_name
                            )
                            print(f"[SP RENAME] {filename} → {final_name}")

                            # 3b. Rename the paired .txt file (same stem)
                            txt_version = sp.find_txt_version(role_hint, filename)
                            if txt_version:
                                txt_new_name = f"{clean}_{job_code}.txt"
                                txt_final, txt_status = sp.rename_item(
                                    txt_version["id"], txt_new_name
                                )
                                if txt_status == "OK":
                                    print(
                                        f"[SP RENAME] {txt_version['name']} → {txt_final}"
                                    )
                                else:
                                    print(
                                        f"[SP RENAME ERROR] TXT rename failed for "
                                        f"{txt_version['name']}: {txt_status}"
                                    )
                            else:
                                print(
                                    f"[SP RENAME] No paired .txt found for {filename}"
                                )
                        else:
                            print(f"[SP RENAME ERROR] {filename}: {status}")
            else:
                reasons = []
                if not is_blank_score:
                    reasons.append(f"score already set ({existing_score})")
                if source == "website":
                    reasons.append("Source='Website'")
                elif not source:
                    reasons.append("Source unknown/empty")
                if reasons:
                    print(f"[SP RENAME] Skipped ({', '.join(reasons)}) for {filename}")

        except Exception as e:
            print(f"[SP ERROR] Background sync failed for {filename}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════


@api_analysis_bp.route("/api/progress")
@login_required
def api_progress():
    """SSE endpoint for real-time progress updates specific to the current user."""
    user_id = current_user.id

    def generate():
        while True:
            prog = get_progress(user_id)
            yield f"data: {json.dumps(prog)}\n\n"
            if prog.get("percent") >= 100:
                set_progress(user_id, 0, "Waiting...")
                break
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


@api_analysis_bp.route("/api/analyze/bulk", methods=["POST"])
@login_required
def api_analyze_bulk():
    """
    Bulk-analyze all resumes in a matched folder against a JD.
    Returns SSE to track progress per candidate.
    """
    data = request.json or {}
    jd_id = data.get("jd_id")
    jd_name = data.get("jd_name", "")
    folder_name = data.get("folder_name", "")
    resume_list = data.get("resumes", [])
    sync_sp = data.get("sync_sharepoint", True)

    if not jd_id or not folder_name or not resume_list:
        return jsonify({"error": "Missing jd_id, folder_name, or resumes"}), 400

    app = current_app._get_current_object()

    def generate():
        with app.app_context():
            sp = SharePointMatchScoreUpdater()

            # 1. Download JD
            try:
                jd_text = sp.download_text_content(jd_id)
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': f'Failed to download JD: {e}'})}\n\n"
                return

            jd_title = folder_name

            try:
                job_code = extract_job_code(jd_title)
            except ValueError:
                job_code = 0

            upsert_job(
                job_id=job_code,
                jd_filename=jd_name,
                role_name=jd_title,
                jd_text=jd_text,
            )

            # Compute "ScreenedWith" once — same for every resume in this bulk run
            screened_with = _compute_screened_with(jd_name)

            to_process = [r for r in resume_list if (r.get("match_score") or 0) == 0]
            skipped_count = len(resume_list) - len(to_process)
            total = len(to_process)

            yield f"data: {json.dumps({'type': 'init', 'total': total, 'skipped': skipped_count})}\n\n"

            if total == 0:
                yield f"data: {json.dumps({'type': 'done', 'message': 'All resumes already analysed.'})}\n\n"
                return

            # 2. Loop Resumes
            for idx, resume_info in enumerate(to_process, 1):
                pdf_item_id = resume_info["id"]
                txt_item_id = resume_info.get("txt_id")
                resume_name = resume_info["name"]
                is_rescore = resume_info.get("_is_rescore", False)
                previous_score = resume_info.get("previous_score", None)
                reviewer_feedback = resume_info.get("reviewer_feedback", None)

                yield f"data: {json.dumps({'type': 'progress', 'current': idx, 'total': total, 'resume_name': resume_name})}\n\n"

                if not txt_item_id:
                    yield f"data: {json.dumps({'type': 'error_item', 'current': idx, 'total': total, 'resume_name': resume_name, 'error': 'No corresponding .txt file found. Please run the text extraction pipeline first.'})}\n\n"
                    continue

                try:
                    # Snapshot old breakdown BEFORE save_candidate overwrites it
                    old_breakdown = None
                    if is_rescore:
                        old_breakdown = get_breakdown_by_resume(resume_name, job_code)

                    resume_text = sp.download_text_content(txt_item_id)

                    analysis_dict = evaluate_resume(
                        resume_text,
                        jd_text,
                        previous_score=previous_score if is_rescore else None,
                        reviewer_feedback=reviewer_feedback if is_rescore else None,
                    )

                    score = analysis_dict.get("function_1_resume_jd_matching", {}).get(
                        "overall_match_score", 0
                    )
                    extraction = analysis_dict.get(
                        "function_2_resume_data_extraction", {}
                    )
                    personal = extraction.get("personal_information", {})

                    # save_candidate writes the NEW breakdown to DB
                    cid = save_candidate(
                        job_id=job_code,
                        result=analysis_dict,
                        resume_filename=resume_name,
                        source=resume_info.get("source", ""),
                    )

                    # SP sync for non-rescored candidates only
                    if sync_sp and not is_rescore:
                        sp_metadata = {
                            "MatchScore": score,
                            "CandidateName": personal.get("full_name", "Unknown"),
                            "CandidateEmail": personal.get("email", ""),
                            "CandidatePhone": personal.get("phone", ""),
                            "ScreenedWith": screened_with,
                        }
                        threading.Thread(
                            target=_background_sp_push,
                            args=(
                                app,
                                resume_name,
                                sp_metadata,
                                jd_title,
                                pdf_item_id,
                                cid,
                                job_code,
                            ),
                            daemon=True,
                        ).start()

                    # Build new match_details for the SSE payload
                    new_match_details = {
                        k: {
                            "status": analysis_dict.get(
                                "function_1_resume_jd_matching", {}
                            )
                            .get(k, {})
                            .get("status", ""),
                            "summary": analysis_dict.get(
                                "function_1_resume_jd_matching", {}
                            )
                            .get(k, {})
                            .get("summary", ""),
                        }
                        for k in [
                            "experience",
                            "education",
                            "location",
                            "project_history_relevance",
                            "tools_used",
                            "certifications",
                        ]
                    }

                    candidate_payload = {
                        "id": cid,
                        "name": personal.get("full_name", "Unknown"),
                        "email": personal.get("email", ""),
                        "score": score,
                        "resume_filename": resume_name,
                        "resume_id": pdf_item_id,
                        "experience": extraction.get("career_metrics", {}).get(
                            "total_experience_in_years", 0
                        ),
                        "current_title": extraction.get("current_employment", {}).get(
                            "current_job_title", ""
                        ),
                        "match_details": new_match_details,
                    }

                    if is_rescore and previous_score is not None:
                        candidate_payload["previous_score"] = previous_score
                    if is_rescore and reviewer_feedback:
                        candidate_payload["reviewer_feedback"] = reviewer_feedback
                    if is_rescore:
                        candidate_payload["old_breakdown"] = old_breakdown

                    yield f"data: {json.dumps({'type': 'result', 'current': idx, 'total': total, 'candidate': candidate_payload})}\n\n"

                except Exception as e:
                    traceback.print_exc()
                    yield f"data: {json.dumps({'type': 'error_item', 'current': idx, 'total': total, 'resume_name': resume_name, 'error': str(e)})}\n\n"

            yield f"data: {json.dumps({'type': 'done', 'message': f'Bulk analysis complete. Processed {total} resumes.'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@api_analysis_bp.route("/api/rescore/finalize", methods=["POST"])
@login_required
def api_rescore_finalize():
    """
    Finalize the score for a rescored candidate.

    Accepts:
      - final_score      : the chosen integer score
      - score_choice     : 'previous' | 'new' | 'average'
      - reviewer_feedback: the feedback text (saved only when score_choice != 'previous')
      - old_breakdown    : the pre-rescore match_breakdown dict (restored when score_choice == 'previous')
    """
    data = request.json or {}
    candidate_db_id = data.get("candidate_id")
    final_score = data.get("final_score")
    score_choice = data.get("score_choice", "new")
    reviewer_feedback = data.get("reviewer_feedback", "").strip() or None
    old_breakdown = data.get("old_breakdown")
    resume_filename = data.get("resume_filename", "")
    resume_id = data.get("resume_id", "")
    candidate_name = data.get("candidate_name", "Unknown")
    candidate_email = data.get("candidate_email", "")
    candidate_phone = data.get("candidate_phone", "")
    job_code_raw = data.get("job_code", "")
    jd_title = data.get("jd_title", "")

    if candidate_db_id is None or final_score is None:
        return jsonify({"error": "Missing candidate_id or final_score"}), 400

    # Derive the ScreenedWith value from the JD filename
    screened_with = _compute_screened_with(jd_title)

    # Extract numeric job code for the rename step
    try:
        numeric_job_code = extract_job_code(str(job_code_raw))
    except (ValueError, TypeError):
        numeric_job_code = None

    try:
        feedback_to_save = reviewer_feedback if score_choice != "previous" else None

        # 1. Update DB with the user-selected score and breakdown logic
        updated = finalize_rescore(
            candidate_db_id=int(candidate_db_id),
            match_score=int(final_score),
            reviewer_feedback=feedback_to_save,
            old_breakdown=old_breakdown,
            score_choice=score_choice,
        )
        if not updated:
            return jsonify({"error": "Candidate not found in DB"}), 404

        # 2. Push metadata + trigger rename in background
        app = current_app._get_current_object()
        sp_metadata = {
            "MatchScore": int(final_score),
            "CandidateName": candidate_name,
            "CandidateEmail": candidate_email,
            "CandidatePhone": candidate_phone,
            "ScreenedWith": screened_with,
        }

        threading.Thread(
            target=_background_sp_push,
            args=(
                app,
                resume_filename,
                sp_metadata,
                jd_title,
                resume_id,
                int(candidate_db_id) if candidate_db_id else None,
                numeric_job_code,
            ),
            daemon=True,
        ).start()

        return jsonify({"success": True, "final_score": int(final_score)}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
