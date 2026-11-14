"""In-process Gmail scheduler (no separate worker service, Redis, Celery or Pub/Sub).

Started from the FastAPI lifespan only when GMAIL_SCHEDULER_ENABLED=true. Every tick, the process that holds the
MongoDB lease ``gmail-scheduler`` syncs each active connection whose last sync started more than
``gmail_sync_interval_hours`` ago and that does not need the user's attention. Under ``uvicorn --reload`` only the
serving child runs the lifespan; with several workers only the lease owner works, and the per-connection lock still
prevents any overlap with manual syncs. Shutdown cancels the loop and releases the lease (a crash lets it expire).
Synchronization only happens while the backend is running.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.gmail import sync
from app.gmail.connection import now
from app.gmail.models import GmailConnection, SchedulerLease

_log = logging.getLogger(__name__)
LEASE_ID = 'gmail-scheduler'


def _lease_ttl() -> timedelta:
    return timedelta(seconds=max(2 * settings.gmail_scheduler_tick_seconds, 900))


class GmailScheduler:
    def __init__(self) -> None:
        self.owner = uuid.uuid4().hex
        self._task: asyncio.Task | None = None

    async def hold_lease(self) -> bool:
        """Acquire or renew the single scheduler lease (atomic; only one live owner)."""
        t = now()
        try:
            doc = await SchedulerLease.get_pymongo_collection().find_one_and_update(
                {'_id': LEASE_ID, '$or': [{'owner': self.owner}, {'expires_at': {'$lte': t}}]},
                {'$set': {'owner': self.owner, 'expires_at': t + _lease_ttl()}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            return False  # another live process owns it
        return bool(doc and doc.get('owner') == self.owner)

    async def release(self) -> None:
        await SchedulerLease.get_pymongo_collection().delete_one({'_id': LEASE_ID, 'owner': self.owner})

    async def due_connections(self) -> list:
        cutoff = now() - timedelta(hours=settings.gmail_sync_interval_hours)
        cursor = GmailConnection.get_pymongo_collection().find(
            {'status': 'active', 'attention': None,
             '$or': [{'last_sync_started_at': None}, {'last_sync_started_at': {'$lte': cutoff}}]},
            {'_id': 1},
        ).sort('last_sync_started_at', 1).limit(100)
        return [row['_id'] async for row in cursor]

    async def tick(self) -> int:
        if not await self.hold_lease():
            return 0
        ran = 0
        for conn_id in await self.due_connections():
            if not await self.hold_lease():
                break  # lost the lease: another process continues
            if await sync.run_now(conn_id, trigger='scheduled') is not None:
                ran += 1
        return ran

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log.error('Gmail scheduler tick failed (%s).', type(error).__name__)
            await asyncio.sleep(settings.gmail_scheduler_tick_seconds)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        try:
            await self.release()
        except Exception:
            _log.warning('Could not release the Gmail scheduler lease; it will expire.')


scheduler = GmailScheduler()
