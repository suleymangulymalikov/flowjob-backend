"""Gmail endpoints under /api/v1. All are authenticated and owner-scoped except the OAuth callback, which is
bound to its user by the single-use state and to its browser by the HttpOnly cookie."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user
from app.config import settings
from app.applications.router import owned, present_full
from app.gmail import crypto, oauth, reviews, sync
from app.gmail.connection import as_utc, connection_for, disconnect, now
from app.gmail.models import GmailConnection, GmailMessage, GmailReview
from app.integration.router import secure_cookie
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['gmail'])
CurrentUser = Annotated[User, Depends(get_current_user)]
_log = logging.getLogger(__name__)

ERROR_MESSAGES = {
    'reauth_required': 'Google access expired. Reconnect Gmail to continue.',
    'gmail_unavailable': 'Gmail could not be reached. Try Sync now later.',
    'gmail_rejected': 'Gmail refused the request. Reconnect Gmail if this continues.',
    'ai_unavailable': 'Email analysis is unavailable right now. Try Sync now later.',
    'quota_reached': 'Today\'s email analysis limit was reached. Remaining emails continue on the next sync.',
    'sync_error': 'The sync stopped unexpectedly. Try Sync now.',
    'gmail_bad_request': 'Gmail rejected the search. Try Sync now; reconnect Gmail if this continues.',
    'config_error': 'Gmail is misconfigured on this server. Contact the FlowJob administrator.',
}


def gmail_ready() -> bool:
    return settings.gmail_configured and crypto.key_problem() is None


def _iso(value) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


async def gmail_status(user: User) -> dict[str, Any]:
    conn = await connection_for(user.id)
    pending = await GmailReview.find(GmailReview.user_id == user.id, GmailReview.state == 'pending').count()
    base: dict[str, Any] = {'available': gmail_ready(), 'pendingReviewCount': pending}
    if conn is None:
        return {**base, 'connection': {'state': 'not_connected'}, 'sync': None}

    counts = {s: 0 for s in ('discovered', 'processing', 'analyzed', 'failed', 'processed')}
    async for row in await GmailMessage.get_pymongo_collection().aggregate([
        {'$match': {'connection_id': conn.id, 'state': {'$in': list(counts)}}},
        {'$group': {'_id': '$state', 'n': {'$sum': 1}}},
    ]):
        counts[row['_id']] = row['n']
    remaining = counts['discovered'] + counts['processing'] + counts['analyzed']
    lock_until = as_utc(conn.lock_expires_at)
    running = bool(conn.lock_owner and lock_until and lock_until > now())
    # A connection-wide problem (needs the user) wins; otherwise report why the last run stopped early.
    code = conn.attention or (conn.last_run or {}).get('stoppedReason')
    error = {'code': code, 'message': ERROR_MESSAGES.get(code, ERROR_MESSAGES['sync_error'])} \
        if code in ERROR_MESSAGES else None
    return {
        **base,
        'connection': {
            'state': conn.status, 'email': conn.email, 'connectedAt': _iso(conn.connected_at),
            'timezone': conn.timezone,
        },
        'sync': {
            'running': running,
            'phase': conn.phase,
            'caughtUp': conn.backfill_done and remaining == 0 and conn.page_token is None
            and conn.window_after is None,
            'lastSyncAt': _iso(conn.last_sync_finished_at),
            'lastSuccessAt': _iso(conn.last_success_at),
            'remaining': remaining,
            'failed': counts['failed'],
            'processed': counts['processed'],
            'needsAttention': conn.attention,
            'error': error,
            'lastRun': conn.last_run,
        },
    }


@router.get('/gmail')
async def read_gmail(user: CurrentUser):
    return await gmail_status(user)


class OAuthStartInput(BaseModel):
    mode: Literal['connect', 'reconnect'] = 'connect'
    timezone: str | None = Field(default=None, max_length=64)


@router.post('/gmail/oauth/start')
async def oauth_start(data: OAuthStartInput, user: CurrentUser, request: Request, response: Response):
    if not settings.gmail_configured:
        raise HTTPException(503, 'Gmail is not configured on this server.')
    if crypto.key_problem():
        raise HTTPException(503, 'Gmail is misconfigured on this server.')
    try:
        result = await oauth.start(user, data.mode, data.timezone)
    except oauth.OAuthFailure as failure:
        messages = {
            'already_connected': 'Gmail is already connected.',
            'not_connected': 'Connect Gmail first.',
            'disconnect_in_progress': 'Gmail is being disconnected. Try again in a moment.',
        }
        raise HTTPException(409, messages.get(failure.code, 'Could not start Google authorization.')) from None
    response.set_cookie(
        oauth.COOKIE, result.cookie_value, max_age=int(oauth.STATE_TTL.total_seconds()), path=oauth.COOKIE_PATH,
        httponly=True, secure=secure_cookie(request), samesite='lax',
    )
    return {'authorizationUrl': result.url}


def _frontend(params: dict[str, str]) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}/app/applications?{urlencode(params)}"


@router.get('/gmail/oauth/callback', include_in_schema=False)
async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    flowjob_gmail_oauth: Annotated[str | None, Cookie()] = None,
):
    if not gmail_ready():
        target = _frontend({'gmail': 'error', 'reason': 'not_configured'})
    else:
        try:
            result = await oauth.complete(code=code, state=state, error=error, cookie=flowjob_gmail_oauth)
        except oauth.OAuthFailure as failure:
            target = _frontend({'gmail': 'error', 'reason': failure.code})
        except Exception as error:  # never surface a stack trace or provider detail to the browser
            _log.error('Gmail OAuth callback failed (%s).', type(error).__name__)
            target = _frontend({'gmail': 'error', 'reason': 'exchange_failed'})
        else:
            # Initial 30-day backfill (or resumption after a reconnect) starts right away in the background.
            await sync.launch_sync(result.connection.id, trigger=result.outcome)
            target = _frontend({'gmail': result.outcome})
    redirect = RedirectResponse(target, status_code=303)
    redirect.delete_cookie(oauth.COOKIE, path=oauth.COOKIE_PATH, httponly=True,
                           secure=secure_cookie(request), samesite='lax')
    redirect.headers['Cache-Control'] = 'no-store'
    redirect.headers['Referrer-Policy'] = 'no-referrer'
    return redirect


class SyncInput(BaseModel):
    retryFailed: bool = False  # noqa: N815


@router.post('/gmail/sync', status_code=202)
async def start_sync(user: CurrentUser, data: SyncInput | None = None):
    """Sync now, or Retry sync (requeues failed messages). Runs in the background; poll GET /gmail."""
    conn = await connection_for(user.id)
    if conn is None:
        raise HTTPException(404, 'Gmail is not connected.')
    if conn.status == 'reauth_required':
        raise HTTPException(409, 'Google access expired. Reconnect Gmail to continue.')
    interval = timedelta(seconds=settings.gmail_manual_sync_min_interval_seconds)
    t = now()
    claimed = await GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn.id, 'status': 'active',
         '$or': [{'last_manual_sync_at': None}, {'last_manual_sync_at': {'$lte': t - interval}}]},
        {'$set': {'last_manual_sync_at': t}},
    )
    if claimed.modified_count != 1:
        wait = int(interval.total_seconds())
        raise HTTPException(429, 'A sync was started moments ago. Try again shortly.',
                            headers={'Retry-After': str(wait)})
    started = await sync.launch_sync(conn.id, trigger='manual', retry_failed=bool(data and data.retryFailed))
    if not started:
        # Give the rate-limit slot back: nothing was started.
        await GmailConnection.get_pymongo_collection().update_one(
            {'_id': conn.id, 'last_manual_sync_at': t}, {'$set': {'last_manual_sync_at': None}})
        raise HTTPException(409, 'A Gmail sync is already running.')
    return await gmail_status(user)


@router.delete('/gmail', status_code=204)
async def delete_gmail(user: CurrentUser):
    conn = await GmailConnection.find_one(GmailConnection.user_id == user.id, ignore_cache=True)
    if conn is None:
        raise HTTPException(404, 'Gmail is not connected.')
    await disconnect(conn)


# --- Needs Review --------------------------------------------------------------------------------------------

@router.get('/gmail/reviews')
async def read_reviews(user: CurrentUser):
    return await reviews.list_reviews(user.id)


class ReviewDecision(BaseModel):
    """Optional edits. New application: company, jobTitle, status, dateApplied. Ambiguous match: applicationId
    (an existing application to link) or nothing to create a new one. Status suggestion: status."""

    company: str | None = Field(default=None, max_length=200)
    jobTitle: str | None = Field(default=None, max_length=200)  # noqa: N815
    status: str | None = Field(default=None, max_length=20)
    dateApplied: str | None = Field(default=None, max_length=10)  # noqa: N815
    applicationId: str | None = Field(default=None, max_length=24)  # noqa: N815


@router.post('/gmail/reviews/{review_id}/approve')
async def approve_review(review_id: str, user: CurrentUser, data: ReviewDecision | None = None):
    edits = data.model_dump(exclude_none=True) if data else {}
    return await reviews.approve(review_id, user.id, edits)


@router.post('/gmail/reviews/{review_id}/dismiss')
async def dismiss_review(review_id: str, user: CurrentUser):
    return await reviews.dismiss(review_id, user.id)


# --- Recruitment activity ------------------------------------------------------------------------------------

class ActivityCorrection(BaseModel):
    eventType: str | None = Field(default=None, max_length=40)  # noqa: N815
    scheduledAt: str | None = Field(default=None, max_length=40)  # noqa: N815


@router.patch('/applications/{app_id}/activity/{event_id}')
async def correct_activity(app_id: str, event_id: str, data: ActivityCorrection, user: CurrentUser):
    await reviews.correct_event(app_id, event_id, user.id, data.model_dump(exclude_unset=True))
    return await present_full(await owned(app_id, user), user)


@router.post('/applications/{app_id}/activity/{event_id}/dismiss')
async def dismiss_activity(app_id: str, event_id: str, user: CurrentUser):
    await reviews.dismiss_event(app_id, event_id, user.id)
    return await present_full(await owned(app_id, user), user)
