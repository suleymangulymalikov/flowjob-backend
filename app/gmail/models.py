"""Gmail integration documents. Every record is owner-scoped and, except the scheduler lease, carries the
connection id so disconnecting (or a stale worker after a disconnect) can erase exactly that connection's data.

No document stores a full email body or an access token. The only credential at rest is the AES-GCM encrypted
refresh token on the connection.
"""
from datetime import UTC, datetime
from typing import Any

import pymongo
from beanie import Document, PydanticObjectId
from pydantic import Field


def _now() -> datetime:
    return datetime.now(UTC)


class GmailConnection(Document):
    user_id: PydanticObjectId
    google_sub: str
    email: str
    status: str = 'active'  # active | reauth_required | disconnecting
    enc_refresh_token: str
    scopes: list[str] = Field(default_factory=list)
    timezone: str = 'UTC'
    connected_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # --- synchronization progress (persisted so restarts lose nothing) ---
    phase: str = 'backfill'  # backfill | incremental
    backfill_done: bool = False
    window_after: datetime | None = None  # current enumeration window [after, before)
    window_before: datetime | None = None
    page_token: str | None = None
    cursor_at: datetime | None = None  # end of the last fully enumerated window
    # Set when a connection-wide failure needs the user (manual Sync now / Retry); the scheduler skips it.
    attention: str | None = None
    last_error: dict[str, Any] | None = None  # {code, at}; never provider bodies or email text
    last_run: dict[str, Any] | None = None
    last_sync_started_at: datetime | None = None
    last_sync_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_manual_sync_at: datetime | None = None

    # --- per-connection sync lock with fencing ---
    lock_owner: str | None = None
    lock_expires_at: datetime | None = None

    class Settings:
        name = 'gmail_connections'
        indexes = [
            pymongo.IndexModel([('user_id', pymongo.ASCENDING)], unique=True),
            pymongo.IndexModel([('google_sub', pymongo.ASCENDING)], unique=True),
            pymongo.IndexModel([('status', pymongo.ASCENDING), ('last_sync_started_at', pymongo.ASCENDING)]),
        ]


class GmailOAuthState(Document):
    state_hash: str
    user_id: PydanticObjectId
    mode: str  # connect | reconnect
    nonce_hash: str  # browser-binding cookie digest
    enc_verifier: str  # PKCE verifier, encrypted with the state as associated data
    oidc_nonce: str
    timezone: str = 'UTC'
    expires_at: datetime

    class Settings:
        name = 'gmail_oauth_states'
        indexes = [
            pymongo.IndexModel([('state_hash', pymongo.ASCENDING)], unique=True),
            pymongo.IndexModel([('expires_at', pymongo.ASCENDING)], expireAfterSeconds=0),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING)]),
        ]


class GmailMessage(Document):
    """Processing ledger: one row per Gmail message id seen by a connection. Ids and states only."""

    user_id: PydanticObjectId
    connection_id: PydanticObjectId
    message_id: str
    thread_id: str | None = None
    internal_at: datetime
    # discovered -> processing -> analyzed -> processed; skipped (not a candidate); gone (deleted in Gmail);
    # failed (awaits a manual Retry sync; never replayed by the scheduler).
    state: str = 'discovered'
    claim: str | None = None  # lock owner that claimed it
    error_code: str | None = None
    # Frozen, validated AI output (structured items with short excerpts, no body). Effects are applied from this
    # snapshot, so a retry after a crash never re-asks the model and cannot reorder or duplicate items.
    analysis: dict[str, Any] | None = None
    prompt_version: str | None = None
    # Sanitized outcome of processing: recruitment flag, item count and per-item kind/event/confidence/evidence/
    # action/reason enums. Absent on rows processed before this field existed.
    decision: dict[str, Any] | None = None
    updated_at: datetime = Field(default_factory=_now)

    class Settings:
        name = 'gmail_messages'
        indexes = [
            pymongo.IndexModel([('connection_id', pymongo.ASCENDING), ('message_id', pymongo.ASCENDING)], unique=True),
            pymongo.IndexModel([('connection_id', pymongo.ASCENDING), ('state', pymongo.ASCENDING),
                                ('internal_at', pymongo.ASCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING)]),
        ]


class GmailReview(Document):
    """A Needs Review item. Dismissed rows are kept while connected so the same item is never re-proposed."""

    user_id: PydanticObjectId
    connection_id: PydanticObjectId
    message_id: str
    thread_id: str | None = None
    item_key: str
    kind: str  # new_application | ambiguous_link | status_change | schedule_change
    state: str = 'pending'  # pending | approved | dismissed | superseded | invalidated
    application_id: PydanticObjectId | None = None
    candidate_ids: list[PydanticObjectId] = Field(default_factory=list)
    source_event_id: PydanticObjectId | None = None
    proposal: dict[str, Any] = Field(default_factory=dict)
    excerpt: str | None = None
    internal_at: datetime
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None

    class Settings:
        name = 'gmail_review_items'
        indexes = [
            pymongo.IndexModel([('connection_id', pymongo.ASCENDING), ('message_id', pymongo.ASCENDING),
                                ('item_key', pymongo.ASCENDING), ('kind', pymongo.ASCENDING)], unique=True),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('state', pymongo.ASCENDING),
                                ('internal_at', pymongo.DESCENDING)]),
            pymongo.IndexModel([('user_id', pymongo.ASCENDING), ('application_id', pymongo.ASCENDING),
                                ('state', pymongo.ASCENDING)]),
        ]


class GmailSuppression(Document):
    """Evidence of a Gmail-linked application the user deleted. Blocks recreation from the same evidence."""

    user_id: PydanticObjectId
    connection_id: PydanticObjectId
    message_ids: list[str] = Field(default_factory=list)
    thread_ids: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    employer_norm: str = ''
    title_norm: str = ''
    created_at: datetime = Field(default_factory=_now)

    class Settings:
        name = 'gmail_suppressions'
        indexes = [pymongo.IndexModel([('connection_id', pymongo.ASCENDING)])]


class SchedulerLease(Document):
    id: str  # type: ignore[assignment]
    owner: str
    expires_at: datetime

    class Settings:
        name = 'scheduler_leases'
