"""Atomic daily AI quota shared by all workers using the same MongoDB."""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from beanie import PydanticObjectId
from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import AutoReconnect, DuplicateKeyError, PyMongoError

from app.config import settings
from app.matches.models import MatchUsage

Workflow = Literal['match', 'tailor']
_GLOBAL = PydanticObjectId('000000000000000000000000')
_log = logging.getLogger(__name__)


def _utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def _day_key(day: str) -> dict:
    return {'user_id': _GLOBAL, 'day': f'ai-quota:{day}'}


async def _ensure_day(day: str) -> None:
    collection = MatchUsage.get_pymongo_collection()
    key = _day_key(day)
    if await collection.find_one(key, {'_id': 1}):
        return

    # Carry forward today's legacy usage on first use. The old documents are read-only.
    match, tailor = {}, {}
    legacy_global = 0
    legacy_keys = [day, f'tailor:{day}', f'ai-global:{day}']
    async for row in collection.find({'day': {'$in': legacy_keys}}, {'user_id': 1, 'day': 1, 'used': 1}):
        used = max(0, row['used'])
        if row['day'] == day:
            match[str(row['user_id'])] = used
        elif row['day'] == f'tailor:{day}':
            tailor[str(row['user_id'])] = used
        elif row['user_id'] == _GLOBAL:
            legacy_global = used
    try:
        await collection.update_one(key, {'$setOnInsert': {
            'used': sum(match.values()) + max(sum(tailor.values()), legacy_global),
            'users': {'match': match, 'tailor': tailor},
            'pending': {},
        }}, upsert=True)
    except DuplicateKeyError:
        # Another worker initialized the day using the unique (user_id, day) index.
        pass


@dataclass(frozen=True)
class Reservation:
    day: str
    user_id: PydanticObjectId
    workflow: Workflow
    token: ObjectId

    @property
    def counter(self) -> str:
        return f'users.{self.workflow}.{self.user_id}'

    @property
    def marker(self) -> str:
        return f'pending.{self.token}'

    async def settle(self, *, refund: bool) -> None:
        # Removing the marker and changing both counters is a single write. Any
        # repeat (including after an ambiguous acknowledgement) is a no-op.
        query = {**_day_key(self.day), self.marker: self.counter}
        update: dict = {'$unset': {self.marker: ''}}
        if refund:
            query.update({'used': {'$gt': 0}, self.counter: {'$gt': 0}})
            update['$inc'] = {'used': -1, self.counter: -1}
        collection = MatchUsage.get_pymongo_collection()
        try:
            await collection.update_one(query, update)
        except AutoReconnect:
            await collection.update_one(query, update)


async def _settle(reservation: Reservation, *, refund: bool) -> None:
    try:
        await reservation.settle(refund=refund)
    except PyMongoError:
        # Retain the charge if the database cannot confirm cleanup. Avoid
        # masking the provider response or logging connection details.
        _log.error('Could not settle an AI quota reservation.')


async def reserve_quota(user_id: PydanticObjectId, workflow: Workflow) -> Reservation:
    day = _utc_day()
    await _ensure_day(day)
    reservation = Reservation(day, user_id, workflow, ObjectId())
    counter = reservation.counter
    user_limit = settings.match_daily_quota if workflow == 'match' else settings.tailor_daily_quota
    global_limit = settings.ai_global_daily_quota
    user_used = {'$ifNull': [f'${counter}', 0]}
    allowed = {'$and': [
        {'$lt': [user_used, user_limit]},
        {'$lt': ['$used', global_limit]},
    ]}

    # Evaluate both limits and increment both counters together. The returned
    # pre-update snapshot is the exact one used to decide whether to admit.
    try:
        before = await MatchUsage.get_pymongo_collection().find_one_and_update(
            _day_key(day),
            [{'$set': {
                'used': {'$cond': [allowed, {'$add': ['$used', 1]}, '$used']},
                counter: {'$cond': [allowed, {'$add': [user_used, 1]}, f'${counter}']},
                reservation.marker: {'$cond': [allowed, counter, '$$REMOVE']},
            }}],
            projection={'used': 1, counter: 1},
            return_document=ReturnDocument.BEFORE,
        )
    except (PyMongoError, asyncio.CancelledError):
        # The write may have succeeded before the connection or task was lost.
        await _settle(reservation, refund=True)
        raise

    if before is None:
        raise HTTPException(503, 'AI quota is temporarily unavailable. Please try again.')
    if before.get('users', {}).get(workflow, {}).get(str(user_id), 0) >= user_limit:
        action = 'AI match' if workflow == 'match' else 'CV tailoring'
        raise HTTPException(429, f'Daily {action} limit reached. Please try again tomorrow.')
    if before['used'] >= global_limit:
        raise HTTPException(503, 'The AI service is at capacity today. Please try again tomorrow.')
    return reservation


@asynccontextmanager
async def generation_quota(user_id: PydanticObjectId, workflow: Workflow):
    try:
        reservation = await reserve_quota(user_id, workflow)
    except PyMongoError:
        raise HTTPException(503, 'AI quota is temporarily unavailable. Please try again.') from None
    try:
        yield
    except BaseException:
        await _settle(reservation, refund=True)
        raise
    else:
        await _settle(reservation, refund=False)
