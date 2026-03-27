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

# Dynamic prompt template — {QA}, {TRANSCRIPT}, {MAX_SCORE} are replaced at runtime.
# This template is used when scoring against a focused (filtered) question set.
DYNAMIC_PROMPT_TEMPLATE = """\
You are an expert Technical Interview Evaluator. Your task is to analyze the provided \
interview call transcript and evaluate the candidate's responses against the provided \
QA reference sheet.

### Instructions:
1. **Analyze Each Question:** For each question, compare the candidate's response to \
the Expected Answer.
2. **Account for Interviewer Guidance:** Candidates who provide accurate and precise \
answers immediately should score higher. Deduct points if the candidate required heavy \
prompting, hints, or corrections from the interviewer to arrive at the correct conclusion.
3. **Scoring:** Grade each question on a scale of 1 to 10 based on the following criteria:
   * **9-10 (Excellent):** Immediately and clearly provides the correct answer with no prompting required.
   * **7-8 (Good):** Provides mostly correct answer on their own, with only a minor hint needed.
   * **4-6 (Partial):** Provides some correct information but required significant prompting, \
OR gave a mostly correct answer but also triggered a Red Flag.
   * **1-3 (Poor):** Missed most key points even after hints, OR clearly demonstrated a Red Flag misunderstanding.
   * **0 (Fail):** Did not answer, said "I don't know", or gave a response matching a Red Flag completely without correction.
4. **Format:** Output your evaluation strictly following the format block below. \
The maximum total score is {MAX_SCORE} (10 points × {QUESTION_COUNT} questions asked).

### Input Data:

<QA_REFERENCE>
{QA}
</QA_REFERENCE>

<TRANSCRIPT>
{TRANSCRIPT}
</TRANSCRIPT>

### Output Format:

For each question evaluated, use this block:

**Question N: [Question Topic/Title]**
* **Candidate's Initial Answer:** [Describe what they said initially, before any prompting]
* **Score:** [Score]/10

---
### **Overall Evaluation**
* **Total Score:** [Total]/{MAX_SCORE}
"""

# ── Global Clients & State ────────────────────────────────────────────────────────────
_SARVAM_JOB_KEYS: dict[str, str] = {}

_CACHED_QA_TEXT: Optional[str] = None
_CACHED_PROMPT_TEMPLATE: Optional[str] = None

_BASE = Path(__file__).resolve().parent.parent.parent

_SARVAM_DONE_STATES = {"Completed"}
_SARVAM_FAIL_STATES = {"Failed"}


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — Sarvam Speech-to-Text-Translate (Batch Job)
# ══════════════════════════════════════════════════════════════════════════════


def start_transcription(audio_path: str) -> str:
    """
    Submits audio to Sarvam STT-Translate batch job and returns job_id immediately.
    """
    if not Config.SARVAM_API_KEYS:
        raise EnvironmentError("No SARVAM_API_KEYs are set in .env")

    print(f"[QA STT] Starting transcription job for: {audio_path}")
    last_exception = None

    for api_key in Config.SARVAM_API_KEYS:
        try:
            client = SarvamAI(api_subscription_key=api_key)
            job = client.speech_to_text_translate_job.create_job(
                model="saaras:v3",
                with_diarization=True,
            )
            job.upload_files(file_paths=[audio_path], timeout=300)
            job.start()

            print(f"[QA STT] Job started → job_id={job.job_id}")
            _SARVAM_JOB_KEYS[job.job_id] = api_key
            return job.job_id
        except Exception as e:
            print(f"[QA STT WARN] API Key failed. Error: {e}. Trying next...")
            last_exception = e

    raise RuntimeError(
        f"[QA STT ERROR] All Sarvam keys failed. Last error: {last_exception}"
    )


def check_transcription_status(job_id: str) -> Tuple[bool, Optional[str]]:
    """
    Polls job status. Returns (is_complete, conversation_text).
    """
    api_key = _SARVAM_JOB_KEYS.get(job_id)
    if not api_key:
        raise RuntimeError(f"Sarvam API key for job {job_id} not found in memory.")

    client = SarvamAI(api_subscription_key=api_key)
    status = client.speech_to_text_translate_job.get_status(job_id=job_id)

    job_state: str = getattr(status, "job_state", None) or ""
    print(f"[QA STT] Job {job_id} → state={job_state!r}")

    if job_state in _SARVAM_FAIL_STATES:
        error_msg = getattr(status, "error_message", "Unknown error")
        raise RuntimeError(f"Sarvam STT job {job_id} failed: {error_msg}")

    if job_state not in _SARVAM_DONE_STATES:
        return False, None

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        job = client.speech_to_text_translate_job.get_job(job_id)
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
            lines = [f"UNKNOWN: {data.get('transcript', '').strip()}"]

        conversation_text = "\n".join(lines)

    return True, conversation_text


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2 — Gemini QA Scoring
# ══════════════════════════════════════════════════════════════════════════════


def _find_project_root() -> Path:
    candidate = _BASE
    for _ in range(4):
        if (candidate / "QA.txt").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return _BASE


def load_prompt_resources() -> Tuple[str, str]:
    """
    Reads QA.txt and prompt_template.txt from disk once, then caches them.
    These are used as the LOCAL fallback only — SharePoint QA files take priority.
    """
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
    max_score: int = None,
) -> dict:
    """
    Send transcript + QA sheet to Gemini for scoring.

    Args:
        transcript:      The diarized call transcript text.
        qa_text:         The focused QA reference text (only asked questions).
                         If empty, falls back to local QA.txt.
        prompt_template: Optional custom prompt template override.
        max_score:       The maximum achievable score (10 × number of asked questions).
                         If None, defaults to 50 (legacy 5-question behaviour).

    Returns:
        {"score_text": str, "token_meta": dict}
    """
    if not Config.GOOGLE_API_KEYS:
        raise EnvironmentError("No GOOGLE_API_KEYs are set in .env")

    # ── Resolve QA text ──────────────────────────────────────────────────────
    # Priority: explicit qa_text arg > local QA.txt fallback
    if qa_text.strip():
        final_qa = qa_text.strip()
    else:
        res_qa, _ = load_prompt_resources()
        final_qa = res_qa

    if not final_qa:
        raise ValueError(
            "QA reference text is empty. "
            "Ensure QA.txt exists at the project root or pass qa_text explicitly."
        )

    # ── Resolve prompt template ──────────────────────────────────────────────
    if prompt_template.strip():
        final_pt = prompt_template.strip()
    else:
        # Always use the dynamic template when scoring from SharePoint QA files
        # so the max score is rendered correctly in the output
        final_pt = DYNAMIC_PROMPT_TEMPLATE

    # ── Compute max_score and question count ─────────────────────────────────
    # Count how many "Question N:" blocks are in the focused QA text
    question_count = len(re.findall(r"(?i)^question\s+\d+\s*:", final_qa, re.MULTILINE))
    if question_count == 0:
        question_count = 5  # safe fallback

    if max_score is None:
        max_score = question_count * 10

    # ── Build prompt ─────────────────────────────────────────────────────────
    prompt = (
        final_pt.replace("{QA}", final_qa)
        .replace("{TRANSCRIPT}", transcript)
        .replace("{MAX_SCORE}", str(max_score))
        .replace("{QUESTION_COUNT}", str(question_count))
    )

    print(
        f"[QA Gemini] Sending {len(prompt)} chars to {GEMINI_MODEL_ID} "
        f"(max_score={max_score}, question_count={question_count})…"
    )

    last_exception = None

    for api_key in Config.GOOGLE_API_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
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
                "max_score": max_score,
                "question_count": question_count,
            }
            print(f"[QA Gemini] Done. tokens={token_meta['total_tokens']}")
            return {"score_text": response.text, "token_meta": token_meta}

        except Exception as e:
            print(f"[QA Gemini WARN] API Key failed. Error: {e}. Trying next...")
            last_exception = e

    raise RuntimeError(
        f"[QA Gemini ERROR] All Google keys failed. Last error: {last_exception}"
    )
