"""Schemas for AI job matching.

`AiAnalysis` is the *structured output* contract the model must return (validated by the
OpenAI SDK and again by Pydantic here). The model only extracts requirements, importance
and grounded evidence — it does NOT invent the numeric score; the backend computes that
deterministically from these fields (see scoring.py).
"""
from enum import Enum

from pydantic import BaseModel, Field

ANALYSIS_VERSION = '1'
PROMPT_VERSION = '1'


class Importance(str, Enum):
    required = 'required'
    preferred = 'preferred'


class EvidenceStatus(str, Enum):
    clearly_demonstrated = 'clearly_demonstrated'
    partially_demonstrated = 'partially_demonstrated'
    not_mentioned = 'not_mentioned'
    uncertain = 'uncertain'


class RequirementAssessment(BaseModel):
    requirement: str = Field(max_length=200)
    importance: Importance
    status: EvidenceStatus
    # Grounded in the CV, or an explicit "Not mentioned in the CV." Never invented.
    evidence: str = Field(max_length=500)


class SuggestedWording(BaseModel):
    section: str = Field(max_length=60)
    suggestion: str = Field(max_length=600)


class AiAnalysis(BaseModel):
    """The model's structured output — requirements + grounded evidence + guidance."""

    enough_info: bool
    overall_assessment: str = Field(max_length=1200)
    requirements: list[RequirementAssessment] = Field(default_factory=list, max_length=40)
    strengths: list[str] = Field(default_factory=list, max_length=12)
    areas_to_improve: list[str] = Field(default_factory=list, max_length=12)
    recommendations: list[str] = Field(default_factory=list, max_length=12)
    suggested_wording: list[SuggestedWording] = Field(default_factory=list, max_length=8)
    clarifying_questions: list[str] = Field(default_factory=list, max_length=8)


# --- API request ---------------------------------------------------------------

class CreateMatch(BaseModel):
    cvId: str
    jobTitle: str = Field(min_length=1, max_length=200)
    jobDescription: str = Field(min_length=1)  # length bounds enforced in the router (configurable)
    company: str | None = Field(default=None, max_length=200)
    jobUrl: str | None = Field(default=None, max_length=2048)
