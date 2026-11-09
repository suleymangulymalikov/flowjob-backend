"""Persisted tailoring drafts (owner-scoped). Stores the structured suggestions and content
hashes for caching/dedup — never the raw job description. Drafts are short-lived review
artifacts; the saved tailored CV is an independent SavedCv created via POST /cvs.
"""
from datetime import UTC, datetime
from typing import Annotated, Any

import pymongo
from beanie import Document, Indexed, PydanticObjectId
from pydantic import Field


class TailorDraft(Document):
    user_id: Annotated[PydanticObjectId, Indexed()]
    source_cv_id: PydanticObjectId
    source_cv_revision: str | None = None  # absent on drafts created before source verification
    match_id: str | None = None
    job_title: str
    company: str | None = None

    cv_hash: str
    job_hash: str
    model: str
    prompt_version: str

    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    missing_qualifications: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)

    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = 'tailor_drafts'
        indexes = [
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('created_at', pymongo.DESCENDING)]),
            pymongo.IndexModel(
                [
                    ('user_id', pymongo.ASCENDING),
                    ('cv_hash', pymongo.ASCENDING),
                    ('job_hash', pymongo.ASCENDING),
                    ('model', pymongo.ASCENDING),
                    ('prompt_version', pymongo.ASCENDING),
                ]
            ),
        ]
