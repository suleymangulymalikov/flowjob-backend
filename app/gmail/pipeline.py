"""Per-message processing: fetch → minimize → quota → classify → validate → freeze → reconcile.

The full message exists only in memory here. The frozen analysis (short verified quotes and structured fields)
is stored on the ledger before effects run, so a crash or retry resumes from it without a second model call.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from app.gmail import classify, reconcile
from app.gmail.google import GoogleError, MessageGone, api
from app.gmail.models import GmailMessage
from app.gmail.preprocess import minimize
from app.gmail.sync import MessageFailed, StopRun, SyncContext
from app.services import ai_gmail
from app.services.ai_provider import AiError, AiNotConfigured, AiRateLimited
from app.services.ai_quota import generation_quota
from app.users.models import User


async def analyze_message(ctx: SyncContext, msg: GmailMessage) -> dict | None:
    conn = ctx.connection
    try:
        full = await api().get_full(await ctx.access(), msg.message_id)
    except MessageGone:
        return None
    except GoogleError as error:
        raise StopRun(error.code, attention=True) from None
    user = await User.get(conn.user_id)
    if user is None:
        raise StopRun('sync_error', attention=True)
    # Every email address in the content is redacted; the user's own name is replaced with [you].
    mini = minimize(full, user_name=user.full_name, user_email=conn.email)
    if not mini.text and not mini.subject:
        return {'items': [], 'is_recruitment': False}
    state = await reconcile.load_state(conn)
    shortlist = reconcile.shortlist(state, msg.thread_id, f'{mini.subject} {mini.sender_name} {mini.text}')
    try:
        zone = ZoneInfo(conn.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo('UTC')
    received = reconcile.as_utc(msg.internal_at).astimezone(zone).isoformat()  # lets the model read local dates
    prompt = classify.build_input(mini, received, shortlist)
    try:
        async with generation_quota(conn.user_id, 'gmail'):
            await ctx.fence()
            out = await run_in_threadpool(ai_gmail.analyze, prompt)
    except HTTPException:
        # Per-user or global Gmail cap reached (or the quota store is briefly unavailable): stop, keep work queued.
        raise StopRun('quota_reached', attention=False) from None
    except (AiNotConfigured, AiRateLimited):
        raise StopRun('ai_unavailable', attention=True) from None
    except AiError:
        raise MessageFailed('ai_error') from None
    return classify.validate(out.analysis, mini, shortlist, conn.timezone)


async def process_message(ctx: SyncContext, msg: GmailMessage) -> str:
    analysis = msg.analysis
    if analysis is None:
        analysis = await analyze_message(ctx, msg)
        if analysis is None:
            return 'gone'
        await ctx.fence()
        await GmailMessage.get_pymongo_collection().update_one(
            {'_id': msg.id, 'claim': ctx.owner},
            {'$set': {'analysis': analysis, 'prompt_version': classify.GMAIL_PROMPT_VERSION}})
        msg.analysis = analysis
    effects = await reconcile.apply_analysis(ctx.connection, msg, analysis, ctx.fence)
    # Sanitized decision record: enums, booleans and counts only (no email text, no model output).
    decision = {
        'isRecruitment': bool(analysis.get('is_recruitment', True)),
        'itemCount': len(analysis.get('items') or []),
        'items': effects.decisions[:5],
        'promptVersion': classify.GMAIL_PROMPT_VERSION,
    }
    await ctx.fence()
    await GmailMessage.get_pymongo_collection().update_one(
        {'_id': msg.id, 'claim': ctx.owner}, {'$set': {'decision': decision}})
    s = ctx.summary
    s.imported += effects.imported
    s.linked += effects.linked
    s.activities += effects.activities
    s.reviews += effects.reviews
    return 'processed'
