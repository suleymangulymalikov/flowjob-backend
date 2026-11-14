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

    # --- Gmail provenance (all optional; existing documents need no migration) ---
    source: str = 'manual'  # manual | gmail. Kept after disconnect.
    imported_at: datetime | None = None
    # True while date_applied is the date of an inferring (non-confirmation) Gmail message, not a known submission
    # date. Cleared when the user edits the date.
    date_applied_provisional: bool = False
    # Connection-specific Gmail identity; removed on disconnect.
    gmail_connection_id: PydanticObjectId | None = None
    gmail_source_key: str | None = None  # unique per user: the extracted item (or review) it came from
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    # Time of the last manual (now) or approved (email time) status decision. Older emails cannot suggest changes.
    status_basis_at: datetime | None = None
    suggestion_review_id: PydanticObjectId | None = None  # the single pending Gmail status suggestion
    last_approved_review_id: PydanticObjectId | None = None

    class Settings:
        name = 'applications'
        # Owner-scoped queries plus the status and date fields the UI sorts/filters on.
        indexes = [
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('status', pymongo.ASCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('date_applied', pymongo.DESCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('follow_up_date', pymongo.ASCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('created_at', pymongo.DESCENDING)]),
            pymongo.IndexModel(
                [('user_id', pymongo.ASCENDING), ('gmail_source_key', pymongo.ASCENDING)], unique=True,
                partialFilterExpression={'gmail_source_key': {'$type': 'string'}},
            ),
            pymongo.IndexModel([('gmail_connection_id', pymongo.ASCENDING)], sparse=True),
        ]


class ApplicationEvent(Document):
    """Recruitment activity and status history of one application.

    source: manual (a user status change), gmail (derived from an email; carries a short excerpt and Gmail ids,
    erased on disconnect) or gmail_approved (an approved Gmail status suggestion; no excerpt or Gmail ids, kept).
    """

    user_id: PydanticObjectId
    application_id: PydanticObjectId
    source: str
    event_type: str
    occurred_at: datetime
    status_from: str | None = None
    status_to: str | None = None
    connection_id: PydanticObjectId | None = None
    message_id: str | None = None
    thread_id: str | None = None
    item_key: str | None = None
    excerpt: str | None = None
    references: list[str] = Field(default_factory=list)
    scheduled_at: datetime | None = None
    scheduled_tz: str | None = None
    previous_scheduled_at: datetime | None = None
    state: str = 'active'  # active | dismissed
    corrected: bool = False  # user-edited: later syncs never overwrite it
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = 'application_events'
        indexes = [
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('application_id', pymongo.ASCENDING),
                                ('occurred_at', pymongo.ASCENDING)]),
            pymongo.IndexModel(
                [('user_id', pymongo.ASCENDING), ('message_id', pymongo.ASCENDING), ('item_key', pymongo.ASCENDING)],
                unique=True, partialFilterExpression={'message_id': {'$type': 'string'}},
            ),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('thread_id', pymongo.ASCENDING)], sparse=True),
            pymongo.IndexModel([('connection_id', pymongo.ASCENDING)], sparse=True),
        ]
