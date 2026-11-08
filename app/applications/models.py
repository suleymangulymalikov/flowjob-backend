"""Job application tracking documents, stored in the same MongoDB (Atlas) database.

Additive: this model does not touch existing users, profiles, CVs or refresh tokens.
"""
from datetime import UTC, date, datetime
from enum import Enum
from typing import Annotated

import pymongo
from beanie import Document, Indexed, PydanticObjectId
from pydantic import Field


class ApplicationStatus(str, Enum):
    saved = 'Saved'
    applied = 'Applied'
    interview = 'Interview'
    offer = 'Offer'
    rejected = 'Rejected'
    withdrawn = 'Withdrawn'


class WorkArrangement(str, Enum):
    remote = 'Remote'
    hybrid = 'Hybrid'
    onsite = 'On-site'


class Application(Document):
    user_id: Annotated[PydanticObjectId, Indexed()]
    company: str
    job_title: str
    job_url: str | None = None
    location: str | None = None
    work_arrangement: WorkArrangement | None = None
    employment_type: str | None = None
    status: ApplicationStatus = ApplicationStatus.saved
    date_applied: date | None = None
    notes: str | None = None
    cv_id: PydanticObjectId | None = None
    follow_up_date: date | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = 'applications'
        # Owner-scoped queries plus the status and date fields the UI sorts/filters on.
        indexes = [
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('status', pymongo.ASCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('date_applied', pymongo.DESCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('follow_up_date', pymongo.ASCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('created_at', pymongo.DESCENDING)]),
        ]
