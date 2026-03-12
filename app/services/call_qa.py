# app/services/call_qa.py — Non-blocking pipeline for transcribing call audio and scoring it via Gemini.

import os
import json
import re
import tempfile
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from sarvamai import SarvamAI
import google.genai as genai
from google.genai import types

from config import Config
from app.db.qa_results import save_qa_result

GEMINI_MODEL_ID = "gemini-2.5-flash"
DEFAULT_PROMPT_TEMPLATE = (
    "Evaluate this transcript:\n\n{TRANSCRIPT}\n\nAgainst these QA guidelines:\n\n{QA}"
)

# ── Global Clients ────────────────────────────────────────────────────────────
_gemini_client = (
    genai.Client(api_key=Config.GOOGLE_API_KEY) if Config.GOOGLE_API_KEY else None
)
_sarvam_client = (
    SarvamAI(api_subscription_key=Config.SARVAM_API_KEY)
    if Config.SARVAM_API_KEY
    else None
)

_CACHED_QA_TEXT: Optional[str] = None
_CACHED_PROMPT_TEMPLATE: Optional[str] = None

# FIX: _BASE resolves to project root regardless of where this file lives.
# If file is at app/services/call_qa.py → .parent×3 = project root. ✓
# If file is at project root directly   → .parent×1 = project root, so we
# walk upward until we find QA.txt as a safety net (see load_prompt_resources).
_BASE = Path(__file__).resolve().parent.parent.parent

# Sarvam job_state values (mixed-case exactly as returned by the API)
# NOTE: get_status() returns these strings — get_job() does NOT expose job_state.
_SARVAM_DONE_STATES = {"Completed"}
_SARVAM_FAIL_STATES = {"Failed"}


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — Sarvam Speech-to-Text-Translate (Batch Job)
# ══════════════════════════════════════════════════════════════════════════════


def start_transcription(audio_path: str) -> str:
    """
    Submits audio to Sarvam STT-Translate batch job and returns job_id immediately.
    The file is fully uploaded synchronously before this function returns, so the
    caller can safely delete the temp file once this call completes.
    """
    if not _sarvam_client:
        raise EnvironmentError("SARVAM_API_KEY is not set in .env")

    print(f"[QA STT] Starting transcription job for: {audio_path}")

    job = _sarvam_client.speech_to_text_translate_job.create_job(
        model="saaras:v3",
        with_diarization=True,
    )
    # upload_files is synchronous — file is fully sent before we proceed
    job.upload_files(file_paths=[audio_path], timeout=300)
    job.start()

    print(f"[QA STT] Job started → job_id={job.job_id}")
    return job.job_id


def check_transcription_status(job_id: str) -> Tuple[bool, Optional[str]]:
    """
    Polls job status. Returns (is_complete, conversation_text).

    IMPORTANT — Use get_status(job_id=...) for polling, NOT get_job().
    get_job() does not expose a job_state attribute; only get_status() does.

    job_state values per Sarvam API spec:
        'Accepted' | 'Pending' | 'Running' | 'Completed' | 'Failed'
    """
    if not _sarvam_client:
        raise EnvironmentError("SARVAM_API_KEY is not set in .env")

    # ✅ Correct polling method
    status = _sarvam_client.speech_to_text_translate_job.get_status(job_id=job_id)

    job_state: str = getattr(status, "job_state", None) or ""
    print(f"[QA STT] Job {job_id} → state={job_state!r}")

    if job_state in _SARVAM_FAIL_STATES:
        error_msg = getattr(status, "error_message", "Unknown error")
        raise RuntimeError(f"Sarvam STT job {job_id} failed: {error_msg}")

    if job_state not in _SARVAM_DONE_STATES:
        # Still Accepted / Pending / Running — keep polling
        return False, None

    # ── Job Completed — download outputs ─────────────────────────────────────
    # Only use get_job() here (for its download_outputs() helper), never for polling.
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)

        job = _sarvam_client.speech_to_text_translate_job.get_job(job_id)
        job.download_outputs(output_dir=str(temp_dir_path))

        json_files = list(temp_dir_path.glob("*.json"))
        if not json_files:
            raise FileNotFoundError(
                f"No .json transcription files found for completed job {job_id}."
            )

        with open(json_files[0], "r", encoding="utf-8") as f:
            data = json.load(f)

        diarized = data.get("diarized_transcript", {}).get("entries")
        lines: List[str] = []

        if diarized:
            for entry in diarized:
                speaker = entry.get("speaker_id", "UNKNOWN")
                text = entry.get("transcript", "").strip()
                lines.append(f"SPEAKER_{speaker}: {text}")
        else:
            # Fallback: no diarization available
            lines = [f"UNKNOWN: {data.get('transcript', '').strip()}"]

        conversation_text = "\n".join(lines)

    return True, conversation_text


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2 — Gemini QA Scoring
# ══════════════════════════════════════════════════════════════════════════════


def _find_project_root() -> Path:
    """
    Walk upward from _BASE to find the directory that actually contains QA.txt.
    Handles cases where this file lives at the project root or is nested deeper.
    """
    candidate = _BASE
    for _ in range(4):  # search up to 4 levels
        if (candidate / "QA.txt").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:  # filesystem root
            break
        candidate = parent
    return _BASE  # best guess if not found


def load_prompt_resources() -> Tuple[str, str]:
    """Reads QA.txt and prompt_template.txt from disk once, then caches them."""
    global _CACHED_QA_TEXT, _CACHED_PROMPT_TEMPLATE

    if _CACHED_QA_TEXT is not None and _CACHED_PROMPT_TEMPLATE is not None:
        return _CACHED_QA_TEXT, _CACHED_PROMPT_TEMPLATE

    root = _find_project_root()
    qa_path = root / "QA.txt"
    pt_path = root / "prompt_template.txt"

    if qa_path.exists():
        _CACHED_QA_TEXT = qa_path.read_text(encoding="utf-8").strip()
        print(f"[QA] Loaded QA.txt from {qa_path} ({len(_CACHED_QA_TEXT)} chars)")
    else:
        _CACHED_QA_TEXT = ""
        print(f"[QA WARN] QA.txt not found at {qa_path}")

    _CACHED_PROMPT_TEMPLATE = ""
    if pt_path.exists():
        raw_pt = pt_path.read_text(encoding="utf-8").strip()
        # Strip the Python triple-quote wrapper if present: prompt_template = """..."""
        match = re.search(r'"""(.*?)"""', raw_pt, re.DOTALL)
        _CACHED_PROMPT_TEMPLATE = match.group(1).strip() if match else raw_pt
        print(f"[QA] Loaded prompt_template.txt from {pt_path}")
    else:
        print(f"[QA WARN] prompt_template.txt not found at {pt_path}")

    return _CACHED_QA_TEXT, _CACHED_PROMPT_TEMPLATE


def score_transcript(
    transcript: str,
    qa_text: str = "",
    prompt_template: str = "",
) -> dict:
    """
    Send transcript + QA sheet to Gemini for scoring.
    Returns {"score_text": str, "token_meta": dict}.
    """
    if not _gemini_client:
        raise EnvironmentError("GOOGLE_API_KEY is not set in .env")

    res_qa, res_pt = load_prompt_resources()

    final_qa = qa_text.strip() if qa_text.strip() else res_qa
    final_pt = prompt_template.strip() if prompt_template.strip() else res_pt
    if not final_pt:
        final_pt = DEFAULT_PROMPT_TEMPLATE

    if not final_qa:
        raise ValueError(
            "QA reference text is empty. "
            "Ensure QA.txt exists at the project root or pass qa_text explicitly."
        )

    prompt = final_pt.replace("{QA}", final_qa).replace("{TRANSCRIPT}", transcript)

    print(f"[QA Gemini] Sending {len(prompt)} chars to {GEMINI_MODEL_ID}…")
    response = _gemini_client.models.generate_content(
        model=GEMINI_MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.2),
    )

    usage = response.usage_metadata
    token_meta = {
        "prompt_tokens": getattr(usage, "prompt_token_count", "N/A"),
        "candidates_tokens": getattr(usage, "candidates_token_count", "N/A"),
        "total_tokens": getattr(usage, "total_token_count", "N/A"),
        "model": GEMINI_MODEL_ID,
    }
    print(f"[QA Gemini] Done. tokens={token_meta['total_tokens']}")

    return {"score_text": response.text, "token_meta": token_meta}


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC PIPELINE (Background Thread Wrapper)
# ══════════════════════════════════════════════════════════════════════════════


def _background_qa_pipeline(
    candidate_fk: int,
    audio_path: str,
    audio_filename: str,
    qa_text: str,
    prompt_template: str,
):
    """Internal thread target: waits for STT, scores with Gemini, saves to DB."""
    if not _sarvam_client:
        print("[QA ERROR] Background pipeline aborted: SARVAM_API_KEY is not set.")
        return
    try:
        # 1. Start job and block ONLY this background thread until complete
        job_id = start_transcription(audio_path)

        # wait_until_complete() blocks the background thread (not the web server)
        job = _sarvam_client.speech_to_text_translate_job.get_job(job_id)
        job.wait_until_complete()

        # Now safe to call check_transcription_status (uses get_status for state)
        is_complete, conversation_text = check_transcription_status(job_id)
        if not is_complete or not conversation_text:
            print(f"[QA ERROR] Job {job_id} reported complete but returned no text.")
            return

        # 2. Score with Gemini
        scoring_result = score_transcript(conversation_text, qa_text, prompt_template)

        # 3. Persist to database
        save_qa_result(
            candidate_fk=candidate_fk,
            audio_filename=audio_filename,
            stt_job_id=job_id,
            conversation_file="stored_in_db",
            conversation_text=conversation_text,
            score_text=scoring_result["score_text"],
            eval_file="stored_in_db",
            token_meta=scoring_result["token_meta"],
        )
        print(f"[QA] Background pipeline complete for job {job_id}")

    except Exception as e:
        print(f"[QA ERROR] Background pipeline failed: {e}")
    finally:
        # Clean up temp audio file regardless of outcome
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass


def run_qa_pipeline_async(
    candidate_fk: int,
    audio_path: str,
    audio_filename: str,
    qa_text: str = "",
    prompt_template: str = "",
):
    """
    Fire-and-forget: runs the full STT → Gemini → DB pipeline in a daemon thread
    so the HTTP response is returned immediately.
    """
    thread = threading.Thread(
        target=_background_qa_pipeline,
        args=(candidate_fk, audio_path, audio_filename, qa_text, prompt_template),
        daemon=True,
    )
    thread.start()
