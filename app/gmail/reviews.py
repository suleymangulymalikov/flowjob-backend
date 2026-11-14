"""Needs Review actions and recruitment-activity corrections. Every operation is owner-scoped (404 otherwise) and
re-validates state atomically, so double clicks, concurrent tabs and stale screens cannot produce inconsistent data.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.applications.models import Application, ApplicationEvent, ApplicationStatus
from app.gmail import reconcile
from app.gmail.classify import EVENT_TYPES
from app.gmail.connection import as_utc, connection_for, now, purge_if_disconnected
from app.gmail.models import GmailConnection, GmailMessage, GmailReview

_GMAIL_ID = re.compile(r'^[0-9a-f]{8,32}$')


def oid(value: str, what: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except (InvalidId, TypeError, ValueError):
        raise HTTPException(404, f'{what} not found') from None


def gmail_url(conn: GmailConnection | None, connection_id, message_id: str | None) -> str | None:
    """Built only from a validated hex Gmail id and the active connection's own address; never from email content."""
    if not conn or conn.status != 'active' or connection_id != conn.id or not message_id:
        return None
    if not _GMAIL_ID.fullmatch(message_id):
        return None
    return f'https://mail.google.com/mail/?authuser={quote(conn.email, safe="@")}#all/{message_id}'


def _iso(value) -> str | None:
    value = as_utc(value) if isinstance(value, datetime) else value
    return value.isoformat() if value else None


def present_review(r: GmailReview, conn: GmailConnection | None, apps: dict[str, Application]) -> dict[str, Any]:
    def brief(app_id) -> dict[str, Any] | None:
        a = apps.get(str(app_id))
        if not a:
            return None
        return {'id': str(a.id), 'company': a.company, 'jobTitle': a.job_title,
                'status': reconcile.status_value(a),
                'dateApplied': a.date_applied.isoformat() if a.date_applied else None}
    return {
        'id': str(r.id), 'kind': r.kind, 'state': r.state, 'proposal': r.proposal, 'excerpt': r.excerpt,
        'receivedAt': _iso(r.internal_at), 'gmailUrl': gmail_url(conn, r.connection_id, r.message_id),
        'application': brief(r.application_id) if r.application_id else None,
        'candidates': [c for c in (brief(i) for i in r.candidate_ids) if c],
    }


async def list_reviews(user_id) -> list[dict[str, Any]]:
    conn = await connection_for(user_id)
    reviews = await GmailReview.find(GmailReview.user_id == user_id, GmailReview.state == 'pending',
                                     ignore_cache=True).sort('-internal_at').limit(200).to_list()
    ids = {i for r in reviews for i in [r.application_id, *r.candidate_ids] if i}
    apps = {str(a.id): a for a in await Application.find(
        Application.user_id == user_id, {'_id': {'$in': list(ids)}}, ignore_cache=True).to_list()} if ids else {}
    return [present_review(r, conn, apps) for r in reviews]


async def owned_review(review_id: str, user_id) -> GmailReview:
    review = await GmailReview.find_one(GmailReview.id == oid(review_id, 'Review'), GmailReview.user_id == user_id,
                                        ignore_cache=True)
    if review is None:
        raise HTTPException(404, 'Review not found')
    return review


async def _finish(review: GmailReview, state: str) -> None:
    await GmailReview.get_pymongo_collection().update_one(
        {'_id': review.id, 'state': 'pending'}, {'$set': {'state': state, 'decided_at': now()}})


def _clean(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > limit:
        raise HTTPException(422, 'Enter a value between 1 and 200 characters.')
    return text


def _status(value: Any, default: str) -> ApplicationStatus:
    try:
        return ApplicationStatus(value or default)
    except ValueError:
        raise HTTPException(422, 'Choose a valid status.') from None


def _applied(value: Any, default: str | None) -> tuple[date | None, bool]:
    if value in (None, ''):
        return (date.fromisoformat(default) if default else None), True
    try:
        chosen = date.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(422, 'Enter a valid date.') from None
    if chosen > datetime.now(UTC).date():
        raise HTTPException(422, 'The date cannot be in the future.')
    return chosen, False


async def _ledger_item(review: GmailReview) -> dict[str, Any]:
    """Reconstruct the item for event creation. The excerpt lives on the review; no email content is re-read."""
    proposal = review.proposal
    return {
        'key': review.item_key, 'event_type': proposal.get('eventType') or 'application_update',
        'quote': review.excerpt, 'references': proposal.get('references') or [],
        'scheduled_at': proposal.get('scheduledAt'), 'scheduled_tz': proposal.get('scheduledTz'),
        'previous_scheduled_at': proposal.get('previousScheduledAt'),
        'proposed_status': None, 'employer': proposal.get('company'), 'job_title': proposal.get('jobTitle'),
    }


def _message(review: GmailReview) -> GmailMessage:
    return GmailMessage(user_id=review.user_id, connection_id=review.connection_id, message_id=review.message_id,
                        thread_id=review.thread_id, internal_at=review.internal_at)


async def _active_connection(review: GmailReview) -> GmailConnection:
    conn = await GmailConnection.find_one(GmailConnection.id == review.connection_id,
                                          GmailConnection.user_id == review.user_id, ignore_cache=True)
    if conn is None or conn.status == 'disconnecting':
        raise HTTPException(404, 'Review not found')
    return conn


async def approve(review_id: str, user_id, edits: dict[str, Any]) -> dict[str, Any]:
    review = await owned_review(review_id, user_id)
    try:
        result = await _approve(review, edits)
    except BaseException:
        await purge_if_disconnected(review.user_id, review.connection_id)
        raise
    # An approval that raced the user's own disconnect must not leave Gmail ids or excerpts behind.
    if await purge_if_disconnected(review.user_id, review.connection_id):
        raise HTTPException(409, 'Gmail was disconnected, so this suggestion no longer applies.')
    return result


async def _approve(review: GmailReview, edits: dict[str, Any]) -> dict[str, Any]:
    if review.state != 'pending':
        if review.state == 'approved':
            return {'review': review.state}
        raise HTTPException(409, 'This suggestion is no longer pending.')
    conn = await _active_connection(review)
    if review.kind == 'status_change':
        return await _approve_status(review, edits)
    if review.kind == 'schedule_change':
        return await _approve_schedule(review)
    if review.kind == 'ambiguous_link' and edits.get('applicationId'):
        return await _approve_link(review, conn, edits['applicationId'])
    if review.kind in ('new_application', 'ambiguous_link'):
        if review.kind == 'new_application' and edits.get('applicationId'):
            return await _approve_link(review, conn, edits['applicationId'])
        return await _approve_new(review, conn, edits)
    raise HTTPException(409, 'This suggestion cannot be approved.')


async def _approve_new(review: GmailReview, conn: GmailConnection, edits: dict[str, Any]) -> dict[str, Any]:
    p = review.proposal
    company = _clean(edits.get('company', p.get('company')))
    title = _clean(edits.get('jobTitle', p.get('jobTitle')))
    if not company or not title:
        raise HTTPException(422, 'Enter the employer and the job title.')
    status = _status(edits.get('status'), p.get('status') or 'Applied')
    applied, provisional_default = _applied(edits.get('dateApplied'), p.get('dateApplied'))
    provisional = bool(p.get('dateProvisional')) and provisional_default
    key = f'review:{review.id}'
    app = Application(
        user_id=review.user_id, company=company, job_title=title, status=status, date_applied=applied,
        source='gmail', imported_at=now(), date_applied_provisional=provisional, gmail_connection_id=conn.id,
        gmail_source_key=key, gmail_message_id=review.message_id, gmail_thread_id=review.thread_id,
        status_basis_at=review.internal_at,
    )
    try:
        await app.insert()
    except DuplicateKeyError:  # approved concurrently: reuse the same application
        app = await Application.find_one(Application.user_id == review.user_id,
                                         Application.gmail_source_key == key, ignore_cache=True)
    item = await _ledger_item(review)
    await reconcile.upsert_event(conn, _message(review), item, app.id)
    await _finish(review, 'approved')
    return {'review': 'approved', 'applicationId': str(app.id)}


async def _approve_link(review: GmailReview, conn: GmailConnection, application_id: str) -> dict[str, Any]:
    app = await Application.find_one(Application.id == oid(application_id, 'Application'),
                                     Application.user_id == review.user_id, ignore_cache=True)
    if app is None:
        raise HTTPException(404, 'Application not found')
    item = await _ledger_item(review)
    item['proposed_status'] = review.proposal.get('status') if review.proposal.get('status') != 'Applied' else None
    msg = _message(review)
    event = await reconcile.upsert_event(conn, msg, item, app.id)
    await _finish(review, 'approved')
    effects = reconcile.Effects()
    # Linking never changes the status itself; a newer status becomes its own suggestion.
    await reconcile.propose_status(conn, msg, item, app, event, effects)
    return {'review': 'approved', 'applicationId': str(app.id)}


async def _approve_status(review: GmailReview, edits: dict[str, Any]) -> dict[str, Any]:
    p = review.proposal
    target = _status(edits.get('status'), p.get('to'))
    apps = Application.get_pymongo_collection()
    at = as_utc(review.internal_at)
    # One atomic, fully re-validated write: owner, still this suggestion, unchanged current status, and no newer
    # user decision. Concurrent approvals: exactly one matches.
    updated = await apps.find_one_and_update(
        {'_id': review.application_id, 'user_id': review.user_id, 'suggestion_review_id': review.id,
         'status': p.get('from'), '$or': [{'status_basis_at': None}, {'status_basis_at': {'$lt': at}}]},
        {'$set': {'status': target.value, 'status_basis_at': at, 'updated_at': now(),
                  'last_approved_review_id': review.id},
         '$unset': {'suggestion_review_id': ''}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        current = await apps.find_one({'_id': review.application_id, 'user_id': review.user_id},
                                       {'last_approved_review_id': 1})
        if current and current.get('last_approved_review_id') == review.id:
            # Applied by a concurrent approval, or by an earlier attempt that was interrupted before finishing:
            # repair the history entry if that attempt never wrote it, then finish.
            await _ensure_status_event(review, p.get('from'), target.value)
            await _finish(review, 'approved')
            return {'review': 'approved', 'applicationId': str(review.application_id)}
        await _finish(review, 'invalidated')
        raise HTTPException(409, 'This suggestion is out of date: the application changed since it was made.')
    await _ensure_status_event(review, p.get('from'), target.value)
    await _finish(review, 'approved')
    return {'review': 'approved', 'applicationId': str(review.application_id)}


async def _ensure_status_event(review: GmailReview, status_from: str | None, status_to: str) -> None:
    """The approved-status history entry, written at most once per review (marker ``review:<id>``)."""
    marker = f'review:{review.id}'
    if not await ApplicationEvent.find_one(ApplicationEvent.user_id == review.user_id,
                                           ApplicationEvent.item_key == marker).exists():
        await ApplicationEvent(user_id=review.user_id, application_id=review.application_id, source='gmail_approved',
                               event_type='status_change', occurred_at=review.internal_at, status_from=status_from,
                               status_to=status_to, item_key=marker).insert()


async def _approve_schedule(review: GmailReview) -> dict[str, Any]:
    p = review.proposal
    if not p.get('scheduledAt') or review.source_event_id is None:
        raise HTTPException(409, 'This suggestion cannot be approved.')
    result = await ApplicationEvent.get_pymongo_collection().update_one(
        {'_id': review.source_event_id, 'user_id': review.user_id, 'state': 'active'},
        {'$set': {'scheduled_at': datetime.fromisoformat(p['scheduledAt']), 'scheduled_tz': p.get('scheduledTz'),
                  'previous_scheduled_at': datetime.fromisoformat(p['previousScheduledAt'])
                  if p.get('previousScheduledAt') else None}})
    if result.matched_count != 1:
        await _finish(review, 'invalidated')
        raise HTTPException(409, 'This suggestion is out of date.')
    await _finish(review, 'approved')
    return {'review': 'approved', 'applicationId': str(review.application_id)}


async def dismiss(review_id: str, user_id) -> dict[str, Any]:
    review = await owned_review(review_id, user_id)
    if review.state == 'dismissed':
        return {'review': 'dismissed'}
    if review.state != 'pending':
        raise HTTPException(409, 'This suggestion is no longer pending.')
    if review.kind == 'status_change' and review.application_id:
        applied = await Application.get_pymongo_collection().find_one(
            {'_id': review.application_id, 'user_id': user_id, 'last_approved_review_id': review.id},
            {'status': 1})
        if applied:
            # An interrupted approval already changed the status: record it truthfully instead of "dismissed".
            await _ensure_status_event(review, review.proposal.get('from'), applied['status'])
            await _finish(review, 'approved')
            raise HTTPException(409, 'This suggestion was already approved.')
    await _finish(review, 'dismissed')  # kept while connected: the same item is never proposed again
    if review.application_id:
        await Application.get_pymongo_collection().update_one(
            {'_id': review.application_id, 'user_id': user_id, 'suggestion_review_id': review.id},
            {'$unset': {'suggestion_review_id': ''}})
    return {'review': 'dismissed'}


# --- activity --------------------------------------------------------------------------------------------------

async def owned_event(app_id: str, event_id: str, user_id) -> ApplicationEvent:
    event = await ApplicationEvent.find_one(
        ApplicationEvent.id == oid(event_id, 'Activity'), ApplicationEvent.user_id == user_id,
        ApplicationEvent.application_id == oid(app_id, 'Activity'), ignore_cache=True)
    if event is None or event.source != 'gmail':
        raise HTTPException(404, 'Activity not found')
    return event


async def correct_event(app_id: str, event_id: str, user_id, data: dict[str, Any]) -> None:
    event = await owned_event(app_id, event_id, user_id)
    changes: dict[str, Any] = {'corrected': True}
    if 'eventType' in data:
        if data['eventType'] not in EVENT_TYPES or data['eventType'] == 'other':
            raise HTTPException(422, 'Choose a valid activity type.')
        changes['event_type'] = data['eventType']
    if 'scheduledAt' in data:
        value = data['scheduledAt']
        if value is None:
            changes['scheduled_at'] = None
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(422, 'Enter a valid date and time.') from None
            if parsed.tzinfo is None:
                raise HTTPException(422, 'Include a time zone offset.')
            changes['scheduled_at'] = parsed
    await ApplicationEvent.get_pymongo_collection().update_one({'_id': event.id, 'user_id': user_id},
                                                               {'$set': changes})
    await reconcile.invalidate_for_event(user_id, event.id)


async def dismiss_event(app_id: str, event_id: str, user_id) -> None:
    event = await owned_event(app_id, event_id, user_id)
    await ApplicationEvent.get_pymongo_collection().update_one(
        {'_id': event.id, 'user_id': user_id}, {'$set': {'state': 'dismissed', 'corrected': True}})
    await reconcile.invalidate_for_event(user_id, event.id)


def present_event(e: ApplicationEvent, conn: GmailConnection | None) -> dict[str, Any]:
    return {
        'id': str(e.id), 'eventType': e.event_type, 'occurredAt': _iso(e.occurred_at), 'excerpt': e.excerpt,
        'scheduledAt': _iso(e.scheduled_at), 'scheduledTz': e.scheduled_tz,
        'previousScheduledAt': _iso(e.previous_scheduled_at), 'corrected': e.corrected,
        'gmailUrl': gmail_url(conn, e.connection_id, e.message_id),
    }
