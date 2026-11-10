"""Persistence for AI job-match results and per-user usage quota.

Privacy: we store the structured analysis and non-sensitive metadata (job title, company,
URL) plus content *hashes* used for caching — never the raw CV content or the full job
description.
"""
from datetime import UTC, datetime
from typing import Annotated, Any

import pymongo
from beanie import Document, Indexed, PydanticObjectId
from pydantic import Field


class MatchRecord(Document):
    user_id: Annotated[PydanticObjectId, Indexed()]
    cv_id: PydanticObjectId
    cv_name: str
    job_title: str
    company: str | None = None
    job_url: str | None = None

    # Cache key parts (hashes, not raw content).
    cv_hash: str
    cv_revision: str | None = None  # absent on historical reports
    job_hash: str
    model: str
    prompt_version: str

    # The presented, structured analysis (no raw CV/job text).
    result: dict[str, Any]
    score: int | None = None

    # Usage + cost (from provider usage data when available).
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = 'match_results'
        indexes = [
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('created_at', pymongo.DESCENDING)]),
            # Cache lookup: identical CV + job for the same user/model/prompt.
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


class MatchUsage(Document):
    """Atomic per-user, per-UTC-day analysis counter for quota enforcement."""

    user_id: PydanticObjectId
    day: str  # YYYY-MM-DD (UTC)
    used: int = 0

    class Settings:
        name = 'match_usage'
        indexes = [
            pymongo.IndexModel(
                [('user_id', pymongo.ASCENDING), ('day', pymongo.ASCENDING)], unique=True
            ),
        ]
