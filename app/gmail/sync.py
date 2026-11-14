"""Gmail synchronization engine: windowed enumeration, a durable message ledger, and fenced batch processing.

Run outline (always under the connection's lock; every write phase first renews the lock, so a worker whose lock
expired, was taken over or was revoked by a disconnect stops before writing):

1. Recover work a crashed run left ``processing`` (back to ``discovered``/``analyzed``; this is unfinished work,
   not a failure, so it is not a retry).
2. Enumerate one time window ``[after, before)`` page by page. The window and page token are persisted after every
   page, so a restart continues where it stopped. The cursor only advances to ``before`` once the whole window has
   been listed. Backfill starts 30 days before connecting; incremental windows overlap the previous one by
   ``gmail_overlap_days`` and the unique ledger key removes duplicates.
3. Process up to ``gmail_batch_size`` (≤50) eligible candidates per batch, oldest first, for at most
   ``gmail_max_batches_per_run`` (≤4) batches. The rest stays queued for the next run.

States: ``discovered``/``analyzed`` = eligible work; ``processed``/``skipped``/``gone`` = done; ``failed`` =
awaits a manual Retry sync (never requeued by the scheduler); connection ``attention`` = a connection-wide
failure that needs the user, which the scheduler will not repeat.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from beanie import PydanticObjectId
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.applications.models import Application
from app.config import settings
from app.gmail.connection import access_token, acquire_lock, as_utc, now, purge, release_lock, renew_lock
from app.gmail.google import GoogleError, InvalidGrant, MessageGone, api
from app.gmail.models import GmailConnection, GmailMessage, GmailReview
from app.gmail.preprocess import internal_at, is_candidate

_log = logging.getLogger(__name__)

EXCLUDE = '-in:spam -in:trash -in:chats -in:drafts -in:sent'


class Fenced(Exception):
    """This worker no longer holds the connection lock (expired, taken over or disconnected)."""


class StopRun(Exception):
    """Stop the whole run. ``attention`` marks a connection-wide problem that needs a manual sync."""

    def __init__(self, code: str, *, attention: bool):
        super().__init__(code)
        self.code = code
        self.attention = attention


class MessageFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class RunSummary:
    trigger: str = 'manual'
    listed: int = 0
    candidates: int = 0
    processed: int = 0
    imported: int = 0
    linked: int = 0
    activities: int = 0
    reviews: int = 0
    failed: int = 0
    gone: int = 0
    stoppedReason: str | None = None  # noqa: N815 (camelCase is the API shape)
    finishedAt: str | None = None  # noqa: N815


@dataclass
class SyncContext:
    connection: GmailConnection
    owner: str
    token: str
    summary: RunSummary = field(default_factory=RunSummary)

    async def fence(self) -> None:
        if not await renew_lock(self.connection.id, self.owner):
            raise Fenced()


Processor = Callable[[SyncContext, GmailMessage], Awaitable[str]]


async def default_processor(ctx: SyncContext, msg: GmailMessage) -> str:
    from app.gmail.pipeline import process_message
    return await process_message(ctx, msg)


processor: Processor = default_processor
_tasks: set[asyncio.Task] = set()


async def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    """Run in the background of this process (strong reference so the task is not garbage collected).
    Tests replace this with an inline ``await`` for determinism."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def launch_sync(conn_id: PydanticObjectId, *, trigger: str, retry_failed: bool = False) -> bool:
    """Take the lock now (so callers learn about contention immediately) and run in the background."""
    owner = uuid.uuid4().hex
    if not await acquire_lock(conn_id, owner):
        return False
    await spawn(run_locked(conn_id, owner, trigger=trigger, retry_failed=retry_failed))
    return True


async def run_now(conn_id: PydanticObjectId, *, trigger: str, retry_failed: bool = False) -> RunSummary | None:
    """Run inline if the lock is free (used by the scheduler and tests). Returns None on contention."""
    owner = uuid.uuid4().hex
    if not await acquire_lock(conn_id, owner):
        return None
    return await run_locked(conn_id, owner, trigger=trigger, retry_failed=retry_failed)


async def run_locked(conn_id: PydanticObjectId, owner: str, *, trigger: str, retry_failed: bool) -> RunSummary:
    summary = RunSummary(trigger=trigger)
    try:
        await _run(conn_id, owner, summary, retry_failed)
    except Fenced:
        summary.stoppedReason = summary.stoppedReason or 'lock_lost'
        await _after_fence_loss(conn_id)
    except Exception:
        _log.exception('Gmail sync stopped unexpectedly.')
        summary.stoppedReason = 'sync_error'
        await _finish(conn_id, owner, summary, attention='sync_error')
    finally:
        await release_lock(conn_id, owner)
    return summary


async def _after_fence_loss(conn_id: PydanticObjectId) -> None:
    """If the lock was lost to a disconnect, erase anything this worker wrote after the disconnect's purge."""
    doc = await GmailConnection.get_pymongo_collection().find_one({'_id': conn_id}, {'status': 1, 'user_id': 1})
    if doc is None or doc.get('status') == 'disconnecting':
        user_id = doc['user_id'] if doc else None
        if user_id is None:
            # The connection is already gone; its user id is on any ledger row this worker may have written.
            row = await GmailMessage.get_pymongo_collection().find_one({'connection_id': conn_id}, {'user_id': 1})
            row = row or await GmailReview.get_pymongo_collection().find_one({'connection_id': conn_id}, {'user_id': 1})
            row = row or await Application.get_pymongo_collection().find_one(
                {'gmail_connection_id': conn_id}, {'user_id': 1})
            user_id = row['user_id'] if row else None
        if user_id is not None:
            await purge(user_id, conn_id)


async def _finish(conn_id: PydanticObjectId, owner: str, summary: RunSummary, *, attention: str | None = None,
                  success: bool = False, extra: dict[str, Any] | None = None) -> None:
    summary.finishedAt = now().isoformat()
    update: dict[str, Any] = {'last_run': asdict(summary), 'last_sync_finished_at': now(), **(extra or {})}
    if attention:
        update['attention'] = attention
        update['last_error'] = {'code': attention, 'at': now()}
    if success:
        update['last_success_at'] = now()
    await GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn_id, 'lock_owner': owner, 'status': {'$ne': 'disconnecting'}}, {'$set': update})


async def _run(conn_id: PydanticObjectId, owner: str, summary: RunSummary, retry_failed: bool) -> None:
    conn = await GmailConnection.get(conn_id, ignore_cache=True)
    if conn is None or conn.status != 'active':
        raise Fenced()
    coll = GmailConnection.get_pymongo_collection()
    started: dict[str, Any] = {'last_sync_started_at': now()}
    if summary.trigger != 'scheduled':
        started['attention'] = None  # a user-started sync is the manual recovery for a connection-wide failure
    await coll.update_one({'_id': conn_id, 'lock_owner': owner}, {'$set': started})

    messages = GmailMessage.get_pymongo_collection()
    if retry_failed:
        await messages.update_many({'connection_id': conn_id, 'state': 'failed'},
                                   {'$set': {'state': 'discovered', 'error_code': None, 'updated_at': now()}})
    # Unfinished work from a crashed run: resume it (not a failure replay).
    await messages.update_many(
        {'connection_id': conn_id, 'state': 'processing', 'claim': {'$ne': owner}, 'analysis': None},
        {'$set': {'state': 'discovered', 'claim': None}})
    await messages.update_many(
        {'connection_id': conn_id, 'state': 'processing', 'claim': {'$ne': owner}, 'analysis': {'$ne': None}},
        {'$set': {'state': 'analyzed', 'claim': None}})

    try:
        token = await access_token(conn)
    except InvalidGrant:
        summary.stoppedReason = 'reauth_required'
        await _finish(conn_id, owner, summary)
        return
    except GoogleError:
        summary.stoppedReason = 'gmail_unavailable'
        await _finish(conn_id, owner, summary, attention='gmail_unavailable')
        return

    ctx = SyncContext(connection=conn, owner=owner, token=token, summary=summary)
    try:
        enumerated = await _enumerate(ctx)
        await _process(ctx)
    except StopRun as stop:
        summary.stoppedReason = stop.code
        await _finish(conn_id, owner, summary, attention=stop.code if stop.attention else None)
        return

    await ctx.fence()
    remaining = await messages.count_documents(
        {'connection_id': conn_id, 'state': {'$in': ['discovered', 'analyzed', 'processing']}})
    extra: dict[str, Any] = {}
    if enumerated and remaining == 0:
        extra = {'backfill_done': True, 'phase': 'incremental'}
    await _finish(conn_id, owner, summary, success=True, extra=extra)


# --- enumeration -------------------------------------------------------------------------------------------------

def _query(after, before) -> str:
    return f'after:{int(after.timestamp())} before:{int(before.timestamp())} {EXCLUDE}'


async def _enumerate(ctx: SyncContext) -> bool:
    """List the current window. Returns True when the window was fully enumerated in this run."""
    conn_id = ctx.connection.id
    coll = GmailConnection.get_pymongo_collection()
    await ctx.fence()
    conn = GmailConnection.model_validate(await coll.find_one({'_id': conn_id}))
    after, before, page = as_utc(conn.window_after), as_utc(conn.window_before), conn.page_token
    if after is None or before is None:
        cursor = as_utc(conn.cursor_at)
        if cursor is None:
            after = as_utc(conn.connected_at) - timedelta(days=settings.gmail_backfill_days)
        else:
            after = cursor - timedelta(days=settings.gmail_overlap_days)
        before, page = now(), None
        await coll.update_one({'_id': conn_id, 'lock_owner': ctx.owner},
                              {'$set': {'window_after': after, 'window_before': before, 'page_token': None}})

    for _ in range(settings.gmail_max_list_pages_per_run):
        try:
            listing = await api().list_messages(ctx.token, _query(after, before), page)
        except GoogleError as error:
            raise StopRun(error.code if error.code != 'message_gone' else 'gmail_unavailable', attention=True) from None
        await ctx.fence()
        for ref in listing.get('messages') or []:
            await _record(ctx, ref)
        page = listing.get('nextPageToken')
        if page:
            await coll.update_one({'_id': conn_id, 'lock_owner': ctx.owner}, {'$set': {'page_token': page}})
            continue
        # Whole window durably listed: only now may the cursor advance.
        await coll.update_one({'_id': conn_id, 'lock_owner': ctx.owner}, {'$set': {
            'cursor_at': before, 'window_after': None, 'window_before': None, 'page_token': None}})
        return True
    return False


async def _record(ctx: SyncContext, ref: dict[str, Any]) -> None:
    message_id = str(ref.get('id') or '')
    if not message_id or len(message_id) > 64:
        return
    ctx.summary.listed += 1
    messages = GmailMessage.get_pymongo_collection()
    if await messages.find_one({'connection_id': ctx.connection.id, 'message_id': message_id}, {'_id': 1}):
        return  # already known (overlap window, duplicate listing)
    try:
        meta = await api().get_metadata(ctx.token, message_id)
    except MessageGone:
        return  # deleted between listing and fetching
    except GoogleError as error:
        raise StopRun(error.code, attention=True) from None
    candidate = is_candidate(meta, ctx.connection.email)
    ctx.summary.candidates += int(candidate)
    await ctx.fence()
    try:
        await messages.insert_one({
            'user_id': ctx.connection.user_id, 'connection_id': ctx.connection.id, 'message_id': message_id,
            'thread_id': str(meta.get('threadId') or ref.get('threadId') or '')[:64] or None,
            'internal_at': internal_at(meta), 'state': 'discovered' if candidate else 'skipped',
            'claim': None, 'error_code': None, 'analysis': None, 'prompt_version': None, 'updated_at': now(),
        })
    except DuplicateKeyError:
        pass


# --- processing --------------------------------------------------------------------------------------------------

async def _claim(ctx: SyncContext) -> GmailMessage | None:
    doc = await GmailMessage.get_pymongo_collection().find_one_and_update(
        {'connection_id': ctx.connection.id, 'state': {'$in': ['discovered', 'analyzed']}},
        {'$set': {'state': 'processing', 'claim': ctx.owner, 'updated_at': now()}},
        sort=[('internal_at', ASCENDING), ('_id', ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )
    return GmailMessage.model_validate(doc) if doc else None


async def _settle(ctx: SyncContext, msg: GmailMessage, update: dict[str, Any], unset_analysis: bool = False) -> None:
    change: dict[str, Any] = {'$set': {**update, 'claim': None, 'updated_at': now()}}
    if unset_analysis:
        change['$set']['analysis'] = None
    await GmailMessage.get_pymongo_collection().update_one(
        {'_id': msg.id, 'claim': ctx.owner}, change)


async def _process(ctx: SyncContext) -> None:
    for _ in range(settings.gmail_max_batches_per_run):
        handled = 0
        for _ in range(settings.gmail_batch_size):
            await ctx.fence()
            msg = await _claim(ctx)
            if msg is None:
                return
            handled += 1
            try:
                outcome = await processor(ctx, msg)
            except StopRun:
                # Not this message's fault: put it back untouched for the next run.
                back = 'analyzed' if msg.analysis else 'discovered'
                await _settle(ctx, msg, {'state': back})
                raise
            except Fenced:
                raise
            except MessageFailed as failure:
                ctx.summary.failed += 1
                await _settle(ctx, msg, {'state': 'failed', 'error_code': failure.code})
                continue
            except Exception:
                _log.exception('A Gmail message could not be processed.')
                ctx.summary.failed += 1
                await _settle(ctx, msg, {'state': 'failed', 'error_code': 'processing_error'})
                continue
            await ctx.fence()  # never settle after losing the lock; the next owner resumes this message
            if outcome == 'gone':
                ctx.summary.gone += 1
            else:
                ctx.summary.processed += 1
            await _settle(ctx, msg, {'state': 'gone' if outcome == 'gone' else 'processed', 'error_code': None},
                          unset_analysis=True)
        if handled < settings.gmail_batch_size:
            return
