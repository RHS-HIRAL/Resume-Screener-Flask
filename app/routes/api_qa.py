# app/routes/api_qa.py — API endpoints for Call Recording QA with async polling.

import os
import re
import tempfile
import threading
from pathlib import Path
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required

from app.db.candidates import (
    get_candidate_by_visible_id,
    get_all_candidates,
    update_candidate_qa_score,
)
from app.db.qa_results import save_qa_result, get_qa_results_by_candidate_fk
from app.services.call_qa import (
    start_transcription,
    check_transcription_status,
    score_transcript,
)
from app.services.sharepoint import SharePointMatchScoreUpdater

api_qa_bp = Blueprint("api_qa", __name__)


def _background_sp_upload(folder_label: str, filename: str, content: bytes):
    """Fire-and-forget: upload a file to SharePoint without blocking the API."""
    try:
        sp_updater = SharePointMatchScoreUpdater()
        sp_updater.upload_file(
            folder_path=folder_label, filename=filename, content=content
        )
        print(f"[SP SYNC] Uploaded {filename} → {folder_label}")
    except Exception as e:
        print(f"[SP ERROR] Background upload failed for {filename}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTE
# ══════════════════════════════════════════════════════════════════════════════


@api_qa_bp.route("/qa")
@login_required
def qa_page():
    """QA Call Scoring dashboard — only shows 'Selected' candidates."""
    all_c = get_all_candidates(min_score=0)
    candidates = []
    for c in all_c:
        if c.get("selection_status", "").lower() == "selected":
            if c.get("screened_at"):
                c["screened_at"] = c["screened_at"].isoformat()
            if c.get("outreach_sent_at"):
                c["outreach_sent_at"] = c["outreach_sent_at"].isoformat()
            candidates.append(c)

    roles = sorted({c["role_name"] for c in candidates if c.get("role_name")})
    return render_template("qa.html", candidates=candidates, roles=roles)


# ══════════════════════════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════════════════════════


@api_qa_bp.route("/api/qa/transcribe", methods=["POST"])
@login_required
def api_qa_transcribe():
    """
    Stage 1 — Receives audio, kicks off SharePoint upload (background) and
    Sarvam STT job. Returns job_id instantly so the frontend can poll.

    Flow:
      1. Read audio bytes into memory.
      2. Write to a NamedTemporaryFile (delete=False so Sarvam can open it).
      3. Call start_transcription() — synchronously uploads the file to Sarvam,
         then returns a job_id.  The temp file is safe to delete after this.
      4. Delete temp file.
      5. Return 202 with job_id.
    """
    audio_file = request.files.get("audio_file")
    candidate_id = request.form.get("candidate_id")

    if not audio_file or not candidate_id:
        return jsonify({"error": "Missing audio_file or candidate_id"}), 400

    candidate = get_candidate_by_visible_id(candidate_id)
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404

    suffix = Path(audio_file.filename).suffix or ".mp3"

    # Read into memory once — reused for both SharePoint and Sarvam
    audio_content: bytes = audio_file.read()

    # Write to disk so the Sarvam SDK can open the file path
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio_content)
            tmp_path = tmp.name

        # 1. SharePoint upload — fire and forget (non-blocking)
        job_id_val = candidate.get("job_id", "Unknown")
        role_name = candidate.get("role_name", "Unknown").replace(" ", "_")
        candidate_name = candidate.get("full_name", "Unknown").replace(" ", "_")
        folder_label = f"CallRecordings/recordings/{job_id_val}_{role_name}"
        audio_filename = f"{job_id_val}_{candidate_name}{suffix}"

        threading.Thread(
            target=_background_sp_upload,
            args=(folder_label, audio_filename, audio_content),
            daemon=True,
        ).start()

        # 2. Start Sarvam transcription — synchronously uploads, returns job_id
        stt_job_id = start_transcription(tmp_path)

        return jsonify(
            {"success": True, "status": "processing", "job_id": stt_job_id}
        ), 202

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        # Safe to delete: start_transcription() already uploaded the file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


@api_qa_bp.route("/api/qa/status/<string:job_id>", methods=["GET"])
@login_required
def api_qa_status(job_id):
    """
    Frontend polls this every 5 s. Returns:
      {"status": "processing"}                              — still running
      {"status": "completed", "transcript": "<text>"}     — done
      {"error": "<msg>"}                                   — failed
    """
    try:
        is_complete, transcript_text = check_transcription_status(job_id)

        if not is_complete:
            return jsonify({"status": "processing"})

        return jsonify({"status": "completed", "transcript": transcript_text})

    except Exception as e:
        print(f"[QA ERROR] Status check for {job_id} failed: {e}")
        return jsonify({"error": str(e)}), 500


@api_qa_bp.route("/api/qa/evaluate", methods=["POST"])
@login_required
def api_qa_evaluate():
    """
    Stage 2 — Takes the (user-edited) transcript, scores via Gemini, saves to DB.
    """
    data = request.json or {}
    candidate_id = data.get("candidate_id")
    transcript = data.get("transcript", "").strip()

    if not candidate_id or not transcript:
        return jsonify({"error": "Missing candidate_id or transcript"}), 400

    candidate = get_candidate_by_visible_id(str(candidate_id))
    if not candidate:
        return jsonify(
            {"error": f"Candidate '{candidate_id}' not found. Screen the resume first."}
        ), 404

    try:
        # 1. Gemini scoring
        scoring_result = score_transcript(transcript=transcript)
        score_text: str = scoring_result["score_text"]

        # 2. Extract numeric score — handles all Gemini markdown variants:
        #    "**Total Score:** 38/50"
        #    "* **Total Score:** 38/50"
        #    "Total Score: 38"
        #    "**Total Score: 38**"
        m = re.search(r"Total\s+Score[\s\*:]+(\d+)", score_text, re.IGNORECASE)
        raw_score: int = int(m.group(1)) if m else 0
        if not m:
            print(
                f"[QA WARN] Could not extract numeric score from Gemini output:\n"
                f"{score_text[:400]}"
            )

        print(f"[QA] Numeric score extracted: {raw_score}/50")

        # 3. Save to database
        qa_row_id = save_qa_result(
            candidate_fk=candidate["id"],
            audio_filename="",  # audio handled by SharePoint
            stt_job_id="",
            conversation_file="sharepoint",
            conversation_text=transcript,
            score_text=score_text,
            eval_file="db",
            token_meta=scoring_result["token_meta"],
        )

        # 4. Update candidate's QA score field
        update_candidate_qa_score(candidate_id, raw_score)

        return jsonify(
            {
                "success": True,
                "score_text": score_text,
                "numeric_score": raw_score,
                "qa_row_id": qa_row_id,
            }
        )

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@api_qa_bp.route("/api/qa/results/<string:candidate_id>")
@login_required
def api_qa_results(candidate_id):
    """Return all past QA results for a candidate, newest first."""
    try:
        candidate = get_candidate_by_visible_id(candidate_id)
        if not candidate:
            return jsonify({"error": "Candidate not found"}), 404

        results = get_qa_results_by_candidate_fk(candidate["id"])
        for r in results:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()

        return jsonify({"success": True, "results": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
