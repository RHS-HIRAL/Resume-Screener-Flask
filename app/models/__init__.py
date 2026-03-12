# app/models/__init__.py
# Re-export the top-level schema used by ai_evaluator.py:
#   from app.models import ComprehensiveResumeAnalysis

from app.models.schemas import (
    ComprehensiveResumeAnalysis,
    ResumeJDMatch,
    ResumeDataExtraction,
    ParameterMatch,
    PersonalInfo,
    Employment,
    CareerMetrics,
    Socials,
    Education,
)

from app.models.user import User

__all__ = [
    "ComprehensiveResumeAnalysis",
    "ResumeJDMatch",
    "ResumeDataExtraction",
    "ParameterMatch",
    "PersonalInfo",
    "Employment",
    "CareerMetrics",
    "Socials",
    "Education",
    "User",
]
