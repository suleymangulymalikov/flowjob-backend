"""Schemas for AI CV tailoring (AI-first contract).

`TailoringAnalysis` is the model's structured output: a short list of typed change PROPOSALS addressed by CV path
(the same paths the match feature cites, e.g. "summary", "experience.exp-1.bullets.0", "projects.p1.description",
"skills"). The model never copies original text and never labels a change "safe": the backend resolves every
target, fills in the original text, checks reorders are exact permutations and skill additions are backed by a
verbatim CV quotation, and decides which changes may be previewed automatically (verified reorders only).
"""
from typing import Any, Literal

from pydantic import BaseModel, Field

TAILORING_PROMPT_VERSION = 'ai-first-1'
TAILORING_VERSION = '2'
MAX_CHANGES = 12


class ProposedChange(BaseModel):
    op: Literal['rewrite', 'reorder', 'delete', 'add_skill']
    # rewrite: summary | experience.<id>.bullets.<n> | projects.<id>.description
    # reorder: "skills" or "experience.<id>" (that role's bullets)   delete: experience.<id>.bullets.<n> or "skills"
    # add_skill: "skills"
    path: str = Field(max_length=100)
    suggested: str | None = Field(default=None, max_length=800)  # rewrite: new text; add_skill/delete skill: the skill name
    order_skills: list[str] = Field(default_factory=list, max_length=100)  # reorder skills: ALL skills, new order
    order_bullets: list[int] = Field(default_factory=list, max_length=40)  # reorder bullets: ALL current indices, new order
    reason: str = Field(max_length=240)  # why it helps for this job, addressed to the CV owner
    job_requirement: str | None = Field(default=None, max_length=160)
    evidence_path: str | None = Field(default=None, max_length=100)  # add_skill: where the CV already names the skill
    evidence_quote: str | None = Field(default=None, max_length=240)  # add_skill: verbatim quote from that item


class TailoringAnalysis(BaseModel):
    # An empty list is a valid, successful answer: nothing worth changing.
    changes: list[ProposedChange] = Field(default_factory=list, max_length=MAX_CHANGES)
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
    jobUrl: str | None = Field(default=None, max_length=2048)
    fresh: bool = False


class TailorDraftResponse(BaseModel):
    id: str
    sourceCvId: str
    sourceCvRevision: str | None  # Explicit null identifies a stored legacy draft.
    matchId: str | None
    jobTitle: str
    company: str
    jobUrl: str | None = None
    suggestions: list[dict[str, Any]]
    missingQualifications: list[str]
    questions: list[str]
    tokens: dict[str, int]
    estimatedCostUsd: float
    createdAt: str
    proposalStats: dict[str, Any] | None = None  # {proposed, kept, rejected: {reason: count}}


class VerifiedTailorDraftResponse(TailorDraftResponse):
    sourceCvRevision: str = Field(min_length=1)
