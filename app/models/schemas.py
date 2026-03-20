from typing import Literal, Optional, Annotated
from pydantic import BaseModel, Field


class ParameterMatch(BaseModel):
    status: Literal["Match", "Partial Match", "No Match"] = Field(
        description="Match, Partial Match, or No Match"
    )
    summary: Optional[str] = Field(
        default="",
        description="A 1-line summary explaining why this parameter matches or does not match the JD",
    )


class ResumeJDMatch(BaseModel):
    overall_match_score: int = Field(
        ge=0, le=100, description="Overall match score strictly on a scale of 0 to 100"
    )
    experience: ParameterMatch
    education: ParameterMatch
    location: ParameterMatch
    project_history_relevance: ParameterMatch
    tools_used: ParameterMatch
    certifications: ParameterMatch


class PersonalInfo(BaseModel):
    full_name: str
    location: str
    email: Optional[str] = (
        None  # EmailStr too strict for raw resume text; malformed or missing emails must not crash
    )
    phone: str


class Employment(BaseModel):
    current_job_title: str
    current_organization: str


class CareerMetrics(BaseModel):
    total_experience_in_years: float = Field(ge=0.0)
    total_jobs: int = Field(ge=0)
    technical_skills: Annotated[list[str], Field(default_factory=list, description="List of technical skills and tools identified in the resume.")]
    certificates_name: Annotated[list[str], Field(default_factory=list, description="List of professional certifications and certificate names mentioned in the resume.")]
    relative_years_of_experience: Annotated[int, Field(description="The total number of years of experience the candidate has that are directly relevant to the target job role (as an integer).")]

class Socials(BaseModel):
    # All social links are Optional — most resumes will not have all three
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None


class Education(BaseModel):
    degree: str
    institution: str
    graduation_year: str


class ResumeDataExtraction(BaseModel):
    personal_information: PersonalInfo
    professional_summary: str
    current_employment: Employment
    career_metrics: CareerMetrics
    social_profiles: Socials
    education_history: list[Education]


class ComprehensiveResumeAnalysis(BaseModel):
    function_1_resume_jd_matching: ResumeJDMatch
    function_2_resume_data_extraction: ResumeDataExtraction
