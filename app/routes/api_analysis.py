# app/routes/api_analysis.py — API endpoints for single and bulk AI resume screening using Gemini.

import os
import json
import time
import threading
import traceback
from flask import Blueprint, request, jsonify, Response, current_app
from flask_login import login_required, current_user

from app.db.jobs import upsert_job
from app.db.candidates import save_candidate, update_candidate_match_score
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


def _background_sp_push(
    app, filename: str, metadata: dict, role_hint: str, item_id: str = ""
):
    """Thread target to push MatchScore to SharePoint with safe App Context."""
    with app.app_context():
        try:
            sp = SharePointMatchScoreUpdater()
            sp.push_metadata(
                filename, metadata, role_hint=role_hint, confirmed_item_id=item_id
            )
            print(f"[SP SYNC] Background sync complete for {filename}")
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
                # Reset after completion to prevent stale data on next run
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

    # Capture app context for the generator thread
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

            # Filter out already analyzed resumes based on SP match_score
            to_process = [r for r in resume_list if (r.get("match_score") or 0) == 0]
            skipped_count = len(resume_list) - len(to_process)
            total = len(to_process)

            yield f"data: {json.dumps({'type': 'init', 'total': total, 'skipped': skipped_count})}\n\n"

            if total == 0:
                yield f"data: {json.dumps({'type': 'done', 'message': 'All resumes already analysed.'})}\n\n"
                return

            # 2. Loop Resumes
            for idx, resume_info in enumerate(to_process, 1):
                pdf_item_id = resume_info[
                    "id"
                ]  # <-- The PDF ID (for SharePoint updates)
                txt_item_id = resume_info.get(
                    "txt_id"
                )  # <-- The TXT ID (for LLM reading)
                resume_name = resume_info["name"]
                is_rescore = resume_info.get("_is_rescore", False)
                previous_score = resume_info.get("previous_score", None)
                reviewer_feedback = resume_info.get("reviewer_feedback", None)

                yield f"data: {json.dumps({'type': 'progress', 'current': idx, 'total': total, 'resume_name': resume_name})}\n\n"

                # Check if the text file is missing before pinging the AI
                if not txt_item_id:
                    yield f"data: {json.dumps({'type': 'error_item', 'current': idx, 'total': total, 'resume_name': resume_name, 'error': 'No corresponding .txt file found. Please run the text extraction pipeline first.'})}\n\n"
                    continue

                try:
                    # 1. Download the TEXT file content to pass to the LLM
                    resume_text = sp.download_text_content(txt_item_id)

                    # AI Analysis — pass human feedback for rescores
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

                    # Save to Postgres
                    cid = save_candidate(
                        job_id=job_code,
                        result=analysis_dict,
                        resume_filename=resume_name,
                    )

                    # 2. Sync to SP (Background) — Update the PDF file metadata
                    if sync_sp and not is_rescore:
                        sp_metadata = {
                            "MatchScore": score,
                            "CandidateName": personal.get("full_name", "Unknown"),
                            "CandidateEmail": personal.get("email", ""),
                            "CandidatePhone": personal.get("phone", ""),
                            "JobID": str(job_code) if job_code else "Unknown",
                            "JobRole": jd_title,
                        }
                        threading.Thread(
                            target=_background_sp_push,
                            args=(
                                app,
                                resume_name,
                                sp_metadata,
                                jd_title,
                                pdf_item_id,
                            ),  # <-- Uses pdf_item_id here
                            daemon=True,
                        ).start()

                    # Yield result to frontend
                    candidate_payload = {
                        "id": cid,
                        "name": personal.get("full_name", "Unknown"),
                        "email": personal.get("email", ""),
                        "score": score,
                        "resume_filename": resume_name,
                        "resume_id": pdf_item_id,  # <-- Send back the PDF ID to the UI
                        "experience": extraction.get("career_metrics", {}).get(
                            "total_experience_in_years", 0
                        ),
                        "current_title": extraction.get("current_employment", {}).get(
                            "current_job_title", ""
                        ),
                        "match_details": {
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
                        },
                    }

                    # Include previous_score for rescored candidates
                    if is_rescore and previous_score is not None:
                        candidate_payload["previous_score"] = previous_score

                    result_payload = {
                        "type": "result",
                        "current": idx,
                        "total": total,
                        "candidate": candidate_payload,
                    }
                    yield f"data: {json.dumps(result_payload)}\n\n"

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
    Accepts the user's chosen score, updates the DB, and pushes to SharePoint.
    """
    data = request.json or {}
    candidate_db_id = data.get("candidate_id")
    final_score = data.get("final_score")
    resume_filename = data.get("resume_filename", "")
    resume_id = data.get("resume_id", "")
    candidate_name = data.get("candidate_name", "Unknown")
    candidate_email = data.get("candidate_email", "")
    candidate_phone = data.get("candidate_phone", "")
    job_code = data.get("job_code", "")
    jd_title = data.get("jd_title", "")

    if candidate_db_id is None or final_score is None:
        return jsonify({"error": "Missing candidate_id or final_score"}), 400

    try:
        # 1. Update DB with the user-selected score
        updated = update_candidate_match_score(int(candidate_db_id), int(final_score))
        if not updated:
            return jsonify({"error": "Candidate not found in DB"}), 404

        # 2. Push to SharePoint
        app = current_app._get_current_object()
        sp_metadata = {
            "MatchScore": int(final_score),
            "CandidateName": candidate_name,
            "CandidateEmail": candidate_email,
            "CandidatePhone": candidate_phone,
            "JobID": str(job_code) if job_code else "Unknown",
            "JobRole": jd_title,
        }
        threading.Thread(
            target=_background_sp_push,
            args=(app, resume_filename, sp_metadata, jd_title, resume_id),
            daemon=True,
        ).start()

        return jsonify({"success": True, "final_score": int(final_score)}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
