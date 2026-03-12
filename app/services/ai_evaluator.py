# app/services/ai_evaluator.py — Handles Gemini API interactions for resume-to-JD matching and data extraction.

import json
import google.genai as genai
from google.genai import types
from config import Config
from app.models import ComprehensiveResumeAnalysis

# Removed global client initialization to add api fallback


def evaluate_resume(resume_text: str, jd_text: str) -> dict:
    """
    Runs AI analysis on resume and JD content using Gemini.
    Returns a parsed dictionary conforming to ComprehensiveResumeAnalysis.
    """

    if not Config.GOOGLE_API_KEYS:
        raise EnvironmentError("No GOOGLE_API_KEYs are set in .env")

    prompt = f"""
    You are an expert technical recruiter. Analyze the Resume and JD.
    Return strictly JSON with matching parameters and data extraction.

    Resume: {resume_text}
    JD: {jd_text}
    """
    last_exception = None

    for api_key in Config.GOOGLE_API_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ComprehensiveResumeAnalysis,
                    temperature=0.1,
                ),
            )
            # Parse the JSON string returned by the model into a Python dictionary
            analysis_dict = json.loads(response.text)
            return analysis_dict
        except Exception as e:
            print(f"[AI Evaluator WARN] API Key failed. Error: {e}. Trying next...")
            last_exception = e

    raise RuntimeError(
        f"[AI Evaluator ERROR] All Google keys failed. Last error: {last_exception}"
    )
