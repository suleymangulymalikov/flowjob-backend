"""Connection credentials, the per-connection fenced lock, and disconnect erasure."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from beanie import PydanticObjectId
from pymongo import ReturnDocument

from app.applications.models import Application, ApplicationEvent
from app.config import settings
from app.gmail import crypto
from app.gmail.google import GoogleError, InvalidGrant, api
from app.gmail.models import GmailConnection, GmailMessage, GmailOAuthState, GmailReview, GmailSuppression

_log = logging.getLogger(__name__)

GMAIL_FIELDS = ('gmail_connection_id', 'gmail_source_key', 'gmail_message_id', 'gmail_thread_id',
                'suggestion_review_id', 'last_approved_review_id')


def now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """MongoDB returns naive UTC datetimes; normalize for comparisons."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def connection_for(user_id: PydanticObjectId) -> GmailConnection | None:
    conn = await GmailConnection.find_one(GmailConnection.user_id == user_id, ignore_cache=True)
    return conn if conn and conn.status != 'disconnecting' else None


async def mark_reauth(conn_id: PydanticObjectId) -> None:
    await GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn_id, 'status': 'active'},
        {'$set': {'status': 'reauth_required', 'attention': 'reauth_required',
                  'last_error': {'code': 'reauth_required', 'at': now()}, 'updated_at': now()}},
    )


async def access_token(conn: GmailConnection) -> str:
    """A fresh access token (memory only). Marks the connection reauth_required when Google refuses the grant."""
    aad = crypto.connection_aad(conn.user_id, conn.google_sub)
    refresh = crypto.decrypt(conn.enc_refresh_token, aad)
    try:
        tokens = await api().refresh(refresh)
    except InvalidGrant:
        await mark_reauth(conn.id)
        raise
    rotated = tokens.refresh_token and tokens.refresh_token != refresh
    if rotated or crypto.needs_rotation(conn.enc_refresh_token):
        enc = crypto.encrypt(tokens.refresh_token or refresh, aad)
        await GmailConnection.get_pymongo_collection().update_one(
            {'_id': conn.id, 'google_sub': conn.google_sub, 'status': {'$ne': 'disconnecting'}},
            {'$set': {'enc_refresh_token': enc, 'updated_at': now()}},
        )
    return tokens.access_token


# --- Fenced lock -------------------------------------------------------------------------------------------------

async def acquire_lock(conn_id: PydanticObjectId, owner: str) -> bool:
    """Take the connection's sync lock if it is free or expired. Only one owner can hold it at a time."""
    t = now()
    doc = await GmailConnection.get_pymongo_collection().find_one_and_update(
        {'_id': conn_id, 'status': 'active',
         '$or': [{'lock_owner': None}, {'lock_expires_at': None}, {'lock_expires_at': {'$lte': t}}]},
        {'$set': {'lock_owner': owner, 'lock_expires_at': t + timedelta(seconds=settings.gmail_lock_ttl_seconds)}},
        return_document=ReturnDocument.AFTER,
    )
    return doc is not None


async def renew_lock(conn_id: PydanticObjectId, owner: str) -> bool:
    """Extend the lock only if this owner still holds it, it has not expired and the connection is still active.

    Every write phase of a sync starts with a successful renewal, so a worker that lost its lock (expired, taken
    over or disconnected) stops before writing.
    """
    t = now()
    result = await GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn_id, 'status': 'active', 'lock_owner': owner, 'lock_expires_at': {'$gt': t}},
        {'$set': {'lock_expires_at': t + timedelta(seconds=settings.gmail_lock_ttl_seconds)}},
    )
    return result.matched_count == 1  # an identical millisecond renewal is still a successful match


async def release_lock(conn_id: PydanticObjectId, owner: str) -> None:
    await GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn_id, 'lock_owner': owner}, {'$set': {'lock_owner': None, 'lock_expires_at': None}},
    )


# --- Disconnect --------------------------------------------------------------------------------------------------

async def purge(user_id: PydanticObjectId, connection_id: PydanticObjectId) -> None:
    """Erase one connection's Gmail-derived data. Idempotent; also run by a worker that finds itself fenced out
    after a disconnect, so records it wrote concurrently are erased too.

    Kept: applications (with source='gmail' and imported_at), their manual history and approved status
    decisions (source='gmail_approved', which never hold excerpts or Gmail ids).
    """
    review_ids = [r['_id'] async for r in GmailReview.get_pymongo_collection().find(
        {'connection_id': connection_id}, {'_id': 1})]
    apps = Application.get_pymongo_collection()
    if review_ids:
        await apps.update_many({'user_id': user_id, 'suggestion_review_id': {'$in': review_ids}},
                               {'$unset': {'suggestion_review_id': ''}})
        await apps.update_many({'user_id': user_id, 'last_approved_review_id': {'$in': review_ids}},
                               {'$unset': {'last_approved_review_id': ''}})
    await apps.update_many({'user_id': user_id, 'gmail_connection_id': connection_id},
                           {'$unset': {f: '' for f in GMAIL_FIELDS}})
    await ApplicationEvent.get_pymongo_collection().delete_many(
        {'user_id': user_id, 'connection_id': connection_id, 'source': 'gmail'})
    await GmailReview.get_pymongo_collection().delete_many({'connection_id': connection_id})
    await GmailSuppression.get_pymongo_collection().delete_many({'connection_id': connection_id})
    await GmailMessage.get_pymongo_collection().delete_many({'connection_id': connection_id})


async def purge_if_disconnected(user_id: PydanticObjectId, connection_id: PydanticObjectId) -> bool:
    """Post-write guard for work that raced a disconnect. If the connection is gone or being disconnected, erase
    everything written for it (idempotent, keyed by connection) and return True.

    Running this AFTER a write closes the race: either the disconnect's own purge runs later and removes the write,
    or this check sees the disconnect and removes it.
    """
    doc = await GmailConnection.get_pymongo_collection().find_one(
        {'_id': connection_id, 'user_id': user_id}, {'status': 1})
    if doc is not None and doc.get('status') != 'disconnecting':
        return False
    await purge(user_id, connection_id)
    return True


# A disconnect older than this that still exists was interrupted (crash, restart, database error): the scheduler
# finishes it. Every step below is idempotent, so a retry racing a slow original is harmless.
ABANDONED_DISCONNECT_AFTER = timedelta(minutes=5)


async def disconnect(conn: GmailConnection) -> None:
    """Fence, revoke, erase. Safe to call again for a connection already in ``disconnecting`` (user retry or the
    scheduler's recovery of an interrupted disconnect)."""
    # 1. Fence: no renewal can succeed once the status leaves 'active', so in-flight work stops writing.
    await GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn.id}, {'$set': {'status': 'disconnecting', 'lock_owner': 'disconnect', 'updated_at': now()}})
    await finish_disconnect(conn)


async def finish_disconnect(conn: GmailConnection) -> None:
    pending = await GmailConnection.get_pymongo_collection().find_one(
        {'_id': conn.id, 'status': 'disconnecting'}, {'_id': 1})
    if pending is None:
        # Already finished (or never started) for this connection: only the idempotent, connection-scoped erasure
        # runs. No revocation, so a stale retry can never touch a newer grant.
        await purge(conn.user_id, conn.id)
        return
    # 2. Revoke this user's own grant (best effort; never logs the token or Google's response body).
    try:
        refresh = crypto.decrypt(conn.enc_refresh_token, crypto.connection_aad(conn.user_id, conn.google_sub))
        if await api().revoke(refresh) is False:
            _log.warning('Google did not confirm revocation of a Gmail grant during disconnect.')
    except crypto.TokenCryptoError:
        _log.warning('Could not revoke a Gmail grant during disconnect (credential not decryptable).')
    except GoogleError as error:
        _log.warning('Could not revoke a Gmail grant during disconnect (%s).', error.code)
    # 3. Erase connection data, then the connection (and its encrypted credential) itself. The connection goes
    # last, so an interruption anywhere before leaves it in 'disconnecting' for a retry.
    await purge(conn.user_id, conn.id)
    await GmailOAuthState.get_pymongo_collection().delete_many({'user_id': conn.user_id})
    await GmailConnection.get_pymongo_collection().delete_one({'_id': conn.id, 'status': 'disconnecting'})


async def finish_abandoned_disconnects(limit: int = 20) -> int:
    """Complete disconnects that were interrupted. Returns how many were finished."""
    cutoff = now() - ABANDONED_DISCONNECT_AFTER
    docs = await GmailConnection.get_pymongo_collection().find(
        {'status': 'disconnecting', 'updated_at': {'$lte': cutoff}}).limit(limit).to_list()
    done = 0
    for doc in docs:
        try:
            await finish_disconnect(GmailConnection.model_validate(doc))
            done += 1
        except Exception as error:  # type only: retried on the next tick
            _log.error('Could not finish an interrupted Gmail disconnect (%s).', type(error).__name__)
    return done
