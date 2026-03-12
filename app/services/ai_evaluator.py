# app/services/ai_evaluator.py — Handles Gemini API interactions for resume-to-JD matching and data extraction.

import json
import google.genai as genai
from google.genai import types
from config import Config
from app.models import ComprehensiveResumeAnalysis

# Initialize the client once at the module level to reuse the connection
_client = genai.Client(api_key=Config.GOOGLE_API_KEY)


def evaluate_resume(resume_text: str, jd_text: str) -> dict:
    """
    Runs AI analysis on resume and JD content using Gemini.
    Returns a parsed dictionary conforming to ComprehensiveResumeAnalysis.
    """
    prompt = f"""
    You are an expert technical recruiter. Analyze the Resume and JD.
    Return strictly JSON with matching parameters and data extraction.

    Resume: {resume_text}
    JD: {jd_text}
    """

    response = _client.models.generate_content(
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
