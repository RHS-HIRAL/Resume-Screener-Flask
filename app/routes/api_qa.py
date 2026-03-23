# app/routes/api_qa.py — API endpoints for Call Recording QA with async polling.

import os
import math
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
    update_call_selection_status,
)
from app.db.qa_results import (
    save_qa_result,
    get_qa_results_by_candidate_fk,
    get_all_evaluated_candidates,
    get_latest_evaluation_for_candidate,
    update_call_eval_decision,
)
from app.services.call_qa import (
    start_transcription,
    check_transcription_status,
    score_transcript,
)
from app.services.sharepoint import SharePointMatchScoreUpdater
from config import Config

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
# QA FILE LOADER  (new endpoint)
# ══════════════════════════════════════════════════════════════════════════════


@api_qa_bp.route("/api/qa/load-qa-file", methods=["POST"])
@login_required
def api_load_qa_file():
    """
    Fetches QA_<job_id>.txt from the candidate's job-role subfolder in SharePoint.

    Expected JSON body:
        { "candidate_id": "940201" }

    Returns:
        {
            "success": true,
            "job_id": 9402,
            "role_name": "Python Developer",
            "qa_text": "Question 1: ...\n...",
            "questions": [
                {"number": 1, "text": "What is...", "expected_answer": "...", "red_flags": "..."},
                ...
            ]
        }

    Errors:
        404 if candidate not found
        422 if QA file not found in SharePoint (scoring is blocked)
    """
    data = request.json or {}
    candidate_id = data.get("candidate_id", "").strip()

    if not candidate_id:
        return jsonify({"error": "candidate_id is required"}), 400

    candidate = get_candidate_by_visible_id(candidate_id)
    if not candidate:
        return jsonify({"error": f"Candidate '{candidate_id}' not found"}), 404

    job_id = candidate.get("job_id")
    role_name = candidate.get("role_name", "Unknown")

    # Build the expected SharePoint path:
    # e.g.  JobRoles Data/9456_Python_Developer/QA_9456.txt
    jobs_folder = Config.SHAREPOINT_JOBS_FOLDER.strip("/")

    # Find the matching subfolder name from SharePoint
    try:
        sp = SharePointMatchScoreUpdater()
        subfolders = [
            item for item in sp._list_folder_children(jobs_folder) if "folder" in item
        ]
    except Exception as e:
        return jsonify({"error": f"SharePoint connection failed: {e}"}), 500

    # Match subfolder whose name starts with the job_id
    matched_folder = None
    for sf in subfolders:
        sf_job_prefix = sf["name"].split("_")[0]
        if sf_job_prefix == str(job_id):
            matched_folder = sf["name"]
            break

    if not matched_folder:
        return jsonify(
            {
                "error": f"No SharePoint subfolder found for job_id={job_id} (role: {role_name}). "
                f"Expected a folder starting with '{job_id}_' inside '{jobs_folder}'."
            }
        ), 422

    qa_filename = f"QA_{job_id}.txt"
    qa_folder_path = f"{jobs_folder}/{matched_folder}"

    # Look for the QA file inside the matched subfolder
    try:
        folder_items = sp._list_folder_children(qa_folder_path)
    except Exception as e:
        return jsonify({"error": f"Failed to list folder contents: {e}"}), 500

    qa_item = None
    for item in folder_items:
        if "file" in item and item["name"].lower() == qa_filename.lower():
            qa_item = item
            break

    if not qa_item:
        return jsonify(
            {
                "error": f"QA file '{qa_filename}' not found in SharePoint folder "
                f"'{qa_folder_path}'. Please upload it before scoring.",
                "required_filename": qa_filename,
                "sharepoint_folder": qa_folder_path,
            }
        ), 422

    # Download the QA file content
    try:
        qa_text = sp.download_text_content(qa_item["id"])
    except Exception as e:
        return jsonify({"error": f"Failed to download QA file: {e}"}), 500

    if not qa_text or not qa_text.strip():
        return jsonify({"error": f"QA file '{qa_filename}' is empty"}), 422

    # Parse the question bank into structured objects
    questions = _parse_qa_question_bank(qa_text)

    if not questions:
        return jsonify(
            {
                "error": "Could not parse any questions from the QA file. "
                "Please check the file format."
            }
        ), 422

    return jsonify(
        {
            "success": True,
            "job_id": job_id,
            "role_name": role_name,
            "qa_filename": qa_filename,
            "qa_folder": qa_folder_path,
            "qa_text": qa_text,
            "questions": questions,
        }
    )


def _parse_qa_question_bank(qa_text: str) -> list[dict]:
    """
    Parse the QA question bank text into a list of structured question dicts.

    Expected format:
        Question 1: <question text>
        Expected Answer: <answer text>
        Red Flags: <red flags text>

    Returns list of dicts:
        [{"number": 1, "text": "...", "expected_answer": "...", "red_flags": "..."}, ...]
    """
    questions = []

    # Split by question blocks
    blocks = re.split(r"(?=Question\s+\d+\s*:)", qa_text, flags=re.IGNORECASE)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract question number and text
        q_match = re.match(
            r"Question\s+(\d+)\s*:\s*(.+?)(?=\n|$)", block, re.IGNORECASE
        )
        if not q_match:
            continue

        q_number = int(q_match.group(1))
        q_text = q_match.group(2).strip()

        # Extract Expected Answer
        ea_match = re.search(
            r"Expected\s+Answer\s*:\s*(.+?)(?=\nRed\s+Flags|\nQuestion\s+\d+|$)",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        expected_answer = ea_match.group(1).strip() if ea_match else ""

        # Extract Red Flags
        rf_match = re.search(
            r"Red\s+Flags\s*:\s*(.+?)(?=\nQuestion\s+\d+|$)",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        red_flags = rf_match.group(1).strip() if rf_match else ""

        questions.append(
            {
                "number": q_number,
                "text": q_text,
                "expected_answer": expected_answer,
                "red_flags": red_flags,
            }
        )

    return questions


# ══════════════════════════════════════════════════════════════════════════════
# TRANSCRIPT QUESTION DETECTION  (new endpoint)
# ══════════════════════════════════════════════════════════════════════════════


@api_qa_bp.route("/api/qa/detect-questions", methods=["POST"])
@login_required
def api_detect_questions():
    """
    Uses Groq compound models to auto-detect which questions from the bank
    were asked in the transcript.

    Fallback chain per key: compound-beta → compound-beta-mini.

    Expected JSON body:
        {
            "transcript": "...",
            "questions": [{"number": 1, "text": "..."}, ...]
        }

    Returns:
        {
            "success": true,
            "detected_question_numbers": [1, 3, 4]
        }
    """
    data = request.json or {}
    transcript = data.get("transcript", "").strip()
    questions = data.get("questions", [])

    if not transcript:
        return jsonify({"error": "transcript is required"}), 400
    if not questions:
        return jsonify({"error": "questions list is required"}), 400

    try:
        detected = _detect_asked_questions(transcript, questions)
        return jsonify({"success": True, "detected_question_numbers": detected})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _detect_asked_questions(transcript: str, questions: list[dict]) -> list[int]:
    """
    Use Groq compound models to identify which question numbers from the bank
    were actually asked in the transcript.

    Fallback chain per API key:
        1. compound-beta        (most capable)
        2. compound-beta-mini   (faster, cheaper)

    If all keys × both models fail, raises RuntimeError.
    Returns a list of question numbers (ints) that were covered.
    """
    import json
    from groq import Groq
    from config import Config

    if not Config.GROQ_API_KEYS:
        raise EnvironmentError("No GROQ_API_KEYs are set in .env")

    question_list = "\n".join(f"Question {q['number']}: {q['text']}" for q in questions)

    prompt = (
        "You are analyzing an interview transcript to determine which questions "
        "from a question bank were actually asked during the interview.\n\n"
        f"QUESTION BANK:\n{question_list}\n\n"
        f"TRANSCRIPT:\n{transcript}\n\n"
        "TASK: Identify which question numbers from the question bank were asked "
        "in the transcript (either verbatim or in a similar/paraphrased form).\n\n"
        "Respond with ONLY a valid JSON array of integer question numbers that were asked. "
        "Example: [1, 3, 5]\n"
        "If no questions were detected, respond with: []"
    )

    # Fallback chain: compound-beta first, compound-beta-mini second
    GROQ_MODELS = ["compound-beta", "compound-beta-mini"]

    last_exception = None
    for api_key in Config.GROQ_API_KEYS:
        for model in GROQ_MODELS:
            try:
                client = Groq(api_key=api_key)
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=256,
                )
                raw = response.choices[0].message.content.strip()
                print(f"[QA DETECT] Groq {model} raw response: {raw!r}")

                # Strip markdown code fences if present (```json ... ```)
                raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
                raw = re.sub(r"\s*```$", "", raw)

                result = json.loads(raw)
                if isinstance(result, list):
                    detected = [int(n) for n in result if isinstance(n, (int, float))]
                    print(f"[QA DETECT] Detected questions via {model}: {detected}")
                    return detected
                return []

            except Exception as e:
                print(
                    f"[QA DETECT WARN] Groq {model} failed (key ...{api_key[-4:]}): {e}. Trying next..."
                )
                last_exception = e

    raise RuntimeError(
        f"[QA DETECT ERROR] All Groq keys and models failed. Last error: {last_exception}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TRANSCRIBE  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════


@api_qa_bp.route("/api/qa/transcribe", methods=["POST"])
@login_required
def api_qa_transcribe():
    """
    Stage 1 — Receives audio, kicks off SharePoint upload (background) and
    Sarvam STT job. Returns job_id instantly so the frontend can poll.
    """
    audio_file = request.files.get("audio_file")
    candidate_id = request.form.get("candidate_id")

    if not audio_file or not candidate_id:
        return jsonify({"error": "Missing audio_file or candidate_id"}), 400

    candidate = get_candidate_by_visible_id(candidate_id)
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404

    suffix = Path(audio_file.filename).suffix or ".mp3"
    audio_content: bytes = audio_file.read()

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio_content)
            tmp_path = tmp.name

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

        stt_job_id = start_transcription(tmp_path)

        return jsonify(
            {"success": True, "status": "processing", "job_id": stt_job_id}
        ), 202

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


@api_qa_bp.route("/api/qa/status/<string:job_id>", methods=["GET"])
@login_required
def api_qa_status(job_id):
    """Poll Sarvam transcription status."""
    try:
        is_complete, transcript_text = check_transcription_status(job_id)
        if not is_complete:
            return jsonify({"status": "processing"})
        return jsonify({"status": "completed", "transcript": transcript_text})
    except Exception as e:
        print(f"[QA ERROR] Status check for {job_id} failed: {e}")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# EVALUATE  (updated to accept qa_text + confirmed_questions)
# ══════════════════════════════════════════════════════════════════════════════


@api_qa_bp.route("/api/qa/evaluate", methods=["POST"])
@login_required
def api_qa_evaluate():
    """
    Stage 2 — Takes the (user-edited) transcript, the QA text loaded from
    SharePoint, and the list of confirmed question numbers the interviewer
    actually asked. Scores via Gemini and saves to DB.

    Expected JSON body:
    {
        "candidate_id": "940201",
        "transcript": "...",
        "qa_text": "...",                     ← full QA bank text from SharePoint
        "confirmed_questions": [1, 3, 4],     ← question numbers interviewer confirmed
        "all_questions": [                    ← parsed question objects (for building prompt)
            {"number": 1, "text": "...", "expected_answer": "...", "red_flags": "..."},
            ...
        ]
    }
    """
    data = request.json or {}
    candidate_id = data.get("candidate_id")
    transcript = data.get("transcript", "").strip()
    qa_text_raw = data.get("qa_text", "").strip()
    confirmed_numbers = data.get("confirmed_questions", [])  # list of ints
    all_questions = data.get("all_questions", [])  # list of question dicts

    if not candidate_id or not transcript:
        return jsonify({"error": "Missing candidate_id or transcript"}), 400

    if not qa_text_raw:
        return jsonify({"error": "qa_text is required. Load the QA file first."}), 400

    if not confirmed_numbers:
        return jsonify(
            {
                "error": "confirmed_questions is required. "
                "Select at least one question that was asked."
            }
        ), 400

    candidate = get_candidate_by_visible_id(str(candidate_id))
    if not candidate:
        return jsonify(
            {"error": f"Candidate '{candidate_id}' not found. Screen the resume first."}
        ), 404

    # Filter to only the confirmed questions
    confirmed_q_objects = [
        q for q in all_questions if q.get("number") in confirmed_numbers
    ]
    if not confirmed_q_objects:
        return jsonify(
            {"error": "None of the confirmed_questions matched the all_questions list."}
        ), 400

    # Build a focused QA text containing only the asked questions
    focused_qa_text = _build_focused_qa_text(confirmed_q_objects)
    max_score = len(confirmed_q_objects) * 10

    try:
        scoring_result = score_transcript(
            transcript=transcript,
            qa_text=focused_qa_text,
            max_score=max_score,
        )
        score_text: str = scoring_result["score_text"]

        # Extract numeric score — handles all Gemini markdown variants
        m = re.search(r"Total\s+Score[\s\*:]+(\d+)", score_text, re.IGNORECASE)
        raw_score: int = int(m.group(1)) if m else 0
        if not m:
            print(
                f"[QA WARN] Could not extract numeric score from Gemini output:\n"
                f"{score_text[:400]}"
            )

        # Normalize to 0-100 scale, ceiling-rounded
        if max_score and max_score > 0:
            normalized_score = math.ceil((raw_score / max_score) * 100)
        else:
            normalized_score = 0

        print(f"[QA] Normalized score: {normalized_score}/100 (raw {raw_score}/{max_score})")

        # Save to database — store confirmed question count and max_score in token_meta
        scoring_result["token_meta"]["confirmed_questions"] = confirmed_numbers
        scoring_result["token_meta"]["max_score"] = max_score
        scoring_result["token_meta"]["raw_score"] = raw_score

        qa_row_id = save_qa_result(
            candidate_fk=candidate["id"],
            audio_filename="",
            stt_job_id="",
            conversation_file="sharepoint",
            conversation_text=transcript,
            score_text=score_text,
            eval_file="db",
            token_meta=scoring_result["token_meta"],
        )

        update_candidate_qa_score(candidate_id, normalized_score)

        return jsonify(
            {
                "success": True,
                "score_text": score_text,
                "numeric_score": normalized_score,
                "raw_score": raw_score,
                "max_score": max_score,
                "confirmed_question_count": len(confirmed_q_objects),
                "qa_row_id": qa_row_id,
            }
        )

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _build_focused_qa_text(questions: list[dict]) -> str:
    """
    Build a QA reference string from only the confirmed/asked questions.
    Renumbers them sequentially (1, 2, 3…) so the prompt stays clean.
    """
    lines = []
    for i, q in enumerate(questions, 1):
        lines.append(f"Question {i}: {q['text']}")
        if q.get("expected_answer"):
            lines.append(f"Expected Answer: {q['expected_answer']}")
        if q.get("red_flags"):
            lines.append(f"Red Flags: {q['red_flags']}")
        lines.append("")
    return "\n".join(lines).strip()


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


# ══════════════════════════════════════════════════════════════════════════════
# CALL EVALUATION RESULTS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════


@api_qa_bp.route("/api/qa/evaluated-candidates")
@login_required
def api_evaluated_candidates():
    """Return all candidates with a qa_score for the Eval Results page."""
    try:
        candidates = get_all_evaluated_candidates()
        for c in candidates:
            if c.get("eval_date"):
                c["eval_date"] = c["eval_date"].isoformat()
            # token_meta is JSONB, already a dict
        return jsonify({"success": True, "candidates": candidates})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_qa_bp.route("/api/qa/evaluation-detail/<string:candidate_id>")
@login_required
def api_evaluation_detail(candidate_id):
    """Return full evaluation detail (score_text, transcript, token_meta) for side panel."""
    try:
        candidate = get_candidate_by_visible_id(candidate_id)
        if not candidate:
            return jsonify({"error": "Candidate not found"}), 404

        eval_result = get_latest_evaluation_for_candidate(candidate["id"])
        if not eval_result:
            return jsonify({"error": "No evaluation found for this candidate"}), 404

        if eval_result.get("created_at"):
            eval_result["created_at"] = eval_result["created_at"].isoformat()

        return jsonify({
            "success": True,
            "candidate": {
                "id": candidate["id"],
                "candidate_id": candidate["candidate_id"],
                "full_name": candidate.get("full_name", ""),
                "email": candidate.get("email", ""),
                "role_name": candidate.get("role_name", ""),
                "match_score": candidate.get("match_score"),
                "qa_score": candidate.get("qa_score"),
                "call_selection_status": candidate.get("call_selection_status"),
                "resume_filename": candidate.get("resume_filename", ""),
            },
            "evaluation": eval_result,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_qa_bp.route("/api/qa/set-next-round", methods=["POST"])
@login_required
def api_set_next_round():
    """Deprecated alias — calls set-call-status internally."""
    return api_set_call_status()


@api_qa_bp.route("/api/qa/set-call-status", methods=["POST"])
@login_required
def api_set_call_status():
    """
    Set (or clear) the call selection status for a candidate.
    Body: { "candidate_id": "940201", "status": "online_test" | "technical_round" | "rejected" | null }
    Passing null or omitting status resets to Pending.
    NEVER modifies selection_status — call-round decisions are independent.
    """
    data = request.json or {}
    candidate_id = data.get("candidate_id", "").strip()
    raw_status = data.get("status")  # can be null/None for Pending

    if not candidate_id:
        return jsonify({"error": "candidate_id is required"}), 400

    valid_statuses = {"online_test", "technical_round", "rejected"}
    if raw_status is not None:
        status = str(raw_status).strip().lower()
        if status not in valid_statuses:
            return jsonify({"error": f"Invalid status. Must be one of: {', '.join(valid_statuses)} or null"}), 400
    else:
        status = None  # Pending / undo

    candidate = get_candidate_by_visible_id(candidate_id)
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404

    try:
        update_call_selection_status(candidate["id"], status)
        update_call_eval_decision(candidate["id"], status or "pending")

        threading.Thread(
            target=_background_sp_eval_sync,
            args=(candidate, status),
            daemon=True,
        ).start()

        return jsonify({"success": True, "status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _background_sp_eval_sync(candidate: dict, status: str | None):
    """
    Background: sync CallEvalScore + CallSelectionStatus to SharePoint.
    Always overwrites both fields.
    Targets only the PDF/DOC/DOCX file (not the .txt companion).
    """
    try:
        sp_updater = SharePointMatchScoreUpdater()
        resume_filename = candidate.get("resume_filename", "")
        role_name = candidate.get("role_name", "")

        if not resume_filename:
            print(f"[SP SYNC] Skipped — no resume_filename for {candidate.get('full_name')}")
            return

        # 1. Find matching items in SharePoint
        matches = sp_updater.find_matching_items(resume_filename, role_hint=role_name)

        # 2. Filter to PDF/DOC/DOCX only — skip .txt files
        doc_matches = [
            m for m in matches
            if m["name"].lower().endswith((".pdf", ".doc", ".docx"))
        ]

        if not doc_matches:
            print(f"[SP SYNC] No PDF/DOC/DOCX found in SharePoint for '{resume_filename}'")
            return

        # 3. Use the first matching document file
        item_id = doc_matches[0]["id"]

        sp_label = status.replace("_", " ").title() if status else "Pending"
        status_str, msg, _ = sp_updater.push_metadata(
            filename=resume_filename,
            metadata={
                "CallEvalScore": str(candidate.get("qa_score", "")),
                "CallSelectionStatus": sp_label,
            },
            role_hint=role_name,
            confirmed_item_id=item_id,
            overwrite=True,
        )
        print(f"[SP SYNC] {candidate.get('full_name')}: {status_str} — {msg}")
    except Exception as e:
        print(f"[SP ERROR] Call eval sync failed: {e}")
