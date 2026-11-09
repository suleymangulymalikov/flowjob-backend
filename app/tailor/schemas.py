"""Schemas for AI CV tailoring.

`TailoringAnalysis` is the model's structured output: a bounded list of section-level change
proposals, each tied to an existing CV section and carrying the ORIGINAL text it rewrites.
The backend verifies that `original` identifies an exact source target. Changes beyond
verifiable formatting need explicit review before the frontend applies them.
"""
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

TAILORING_PROMPT_VERSION = '1'
TAILORING_VERSION = '1'


class ChangeSection(str, Enum):
    summary = 'summary'
    experience = 'experience'
    skills = 'skills'
    project = 'project'


class ProposedChange(BaseModel):
    section: ChangeSection
    experience_id: str | None = None
    bullet_index: int | None = None
    project_id: str | None = None
    # The exact existing text being rewritten (used to verify grounding), and the rewrite.
    original: str = Field(max_length=800)
    suggested: str = Field(max_length=800)
    explanation: str = Field(max_length=400)
    job_requirement: str | None = Field(default=None, max_length=200)
    # True when the model is not fully certain the rewrite is supported by the CV.
    review_required: bool = False


class TailoringAnalysis(BaseModel):
    changes: list[ProposedChange] = Field(default_factory=list, max_length=25)
    # Requirements the job needs but the CV does not support — surfaced, never added.
    missing_qualifications: list[str] = Field(default_factory=list, max_length=12)
    clarifying_questions: list[str] = Field(default_factory=list, max_length=8)


class CreateTailor(BaseModel):
    # Either reuse an existing match (preferred — no second analysis), or pass a CV + job directly.
    matchId: str | None = None
    cvId: str | None = None
    jobTitle: str | None = Field(default=None, max_length=200)
    jobDescription: str | None = None
    company: str | None = Field(default=None, max_length=200)
    fresh: bool = False


class TailorDraftResponse(BaseModel):
    id: str
    sourceCvId: str
    sourceCvRevision: str | None  # Explicit null identifies a stored legacy draft.
    matchId: str | None
    jobTitle: str
    company: str
    suggestions: list[dict[str, Any]]
    missingQualifications: list[str]
    questions: list[str]
    tokens: dict[str, int]
    estimatedCostUsd: float
    createdAt: str


class VerifiedTailorDraftResponse(TailorDraftResponse):
    sourceCvRevision: str = Field(min_length=1)
