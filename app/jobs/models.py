"""Cached job postings in Poland, refreshed from public job-board APIs.

JobPosting is shared by all users (jobs are public data); nothing here is user-owned.
JobSource remembers when each company was last fetched and whether it worked.
"""
from datetime import UTC, datetime
from typing import Annotated

import pymongo
from beanie import Document, Indexed
from pydantic import Field


class JobPosting(Document):
    key: Annotated[str, Indexed(unique=True)]  # '<vendor>:<slug>:<external_id>'
    external_id: str
    vendor: str
    slug: str
    company: str
    title: str
    location: str
    city: str  # display city, or 'Poland' when only the country is known
    url: str | None = None
    level: str  # 'internship' | 'entry' | 'other'
    level_signal: str | None = None  # 'api' | 'title' | 'description'
    description: str | None = None  # None until loaded (some vendors need a second request)
    posted_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = 'job_postings'
        indexes = [
            pymongo.IndexModel([('level', pymongo.ASCENDING), ('posted_at', pymongo.DESCENDING)]),
            pymongo.IndexModel([('city', pymongo.ASCENDING)]),
            pymongo.IndexModel([('vendor', pymongo.ASCENDING), ('slug', pymongo.ASCENDING)]),
        ]


class JobSource(Document):
    key: Annotated[str, Indexed(unique=True)]  # '<vendor>:<slug>'
    company: str
    vendor: str
    slug: str
    last_fetched_at: datetime | None = None
    last_ok: bool = False
    last_error: str | None = None
    job_count: int = 0

    class Settings:
        name = 'job_sources'
