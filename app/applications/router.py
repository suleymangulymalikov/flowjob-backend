"""Authenticated, owner-scoped job application endpoints under /api/v1."""
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from beanie import PydanticObjectId
from beanie.odm.queries.update import UpdateResponse
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.applications.models import Application, ApplicationEvent, ApplicationStatus
from app.applications.schemas import ApplicationCreate, ApplicationUpdate
from app.auth.dependencies import get_current_user
from app.integration.models import SavedCv
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['applications'])
CurrentUser = Annotated[User, Depends(get_current_user)]


def present(a: Application) -> dict[str, Any]:
    return {
        'id': str(a.id),
        'company': a.company,
        'jobTitle': a.job_title,
        'jobUrl': a.job_url,
        'location': a.location,
        'workArrangement': a.work_arrangement.value if a.work_arrangement else None,
        'employmentType': a.employment_type,
        'status': a.status.value,
        'dateApplied': a.date_applied.isoformat() if a.date_applied else None,
        'notes': a.notes,
        'cvId': str(a.cv_id) if a.cv_id else None,
        'followUpDate': a.follow_up_date.isoformat() if a.follow_up_date else None,
        'createdAt': a.created_at.isoformat(),
        'updatedAt': a.updated_at.isoformat(),
        'source': a.source,
        'importedAt': a.imported_at.isoformat() if a.imported_at else None,
        'dateAppliedProvisional': a.date_applied_provisional,
        # Filled by present_many/present_full from the status history and the pending Gmail suggestion.
        'history': [],
        'activity': [],
        'suggestion': None,
    }


async def _suggestions(apps: list[Application], user: User) -> dict[str, dict[str, Any]]:
    from app.gmail.connection import connection_for
    from app.gmail.models import GmailReview
    from app.gmail.reviews import gmail_url
    ids = [a.suggestion_review_id for a in apps if a.suggestion_review_id]
    if not ids:
        return {}
    conn = await connection_for(user.id)
    rows = await GmailReview.find(GmailReview.user_id == user.id, GmailReview.state == 'pending',
                                  {'_id': {'$in': ids}}, ignore_cache=True).to_list()
    out = {}
    for r in rows:
        out[str(r.application_id)] = {
            'id': str(r.id), 'proposedStatus': r.proposal.get('to'), 'currentStatus': r.proposal.get('from'),
            'eventType': r.proposal.get('eventType'), 'excerpt': r.excerpt,
            'receivedAt': (r.internal_at if r.internal_at.tzinfo else r.internal_at.replace(tzinfo=UTC)).isoformat(),
            'gmailUrl': gmail_url(conn, r.connection_id, r.message_id),
        }
    return out


async def present_many(apps: list[Application], user: User) -> list[dict[str, Any]]:
    """List and detail share one shape: status history, Gmail recruitment activity and the pending suggestion,
    loaded with one events query for all listed applications."""
    from app.gmail.connection import connection_for
    from app.gmail.reviews import present_event
    if not apps:
        return []
    suggestions = await _suggestions(apps, user)
    conn = await connection_for(user.id)
    events: dict[str, list[ApplicationEvent]] = {}
    for e in await ApplicationEvent.find(ApplicationEvent.user_id == user.id,
                                         {'application_id': {'$in': [a.id for a in apps]}},
                                         ignore_cache=True).sort('occurred_at').to_list():
        events.setdefault(str(e.application_id), []).append(e)
    out = []
    for a in apps:
        item = present(a)
        mine = events.get(str(a.id), [])
        item['suggestion'] = suggestions.get(str(a.id))
        changes = [e for e in mine if e.event_type == 'status_change' and e.status_to]
        if changes:
            # Oldest first: the status the application was added with, then each manual or approved change.
            item['history'] = [{'id': 'created', 'status': changes[0].status_from or item['status'],
                                'at': _utc_iso(a.created_at), 'source': 'manual'}] + [
                {'id': str(e.id), 'status': e.status_to, 'at': _utc_iso(e.occurred_at),
                 'source': 'manual' if e.source == 'manual' else 'gmail_confirmed'}
                for e in changes
            ]
        item['activity'] = [present_event(e, conn) for e in mine if e.source == 'gmail' and e.state == 'active']
        out.append(item)
    return out


def _utc_iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


async def present_full(a: Application, user: User) -> dict[str, Any]:
    return (await present_many([a], user))[0]


def _oid(value: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(404, 'Application not found')


async def owned(app_id: str, user: User) -> Application:
    app = await Application.find_one(Application.id == _oid(app_id), Application.user_id == user.id)
    if not app:
        raise HTTPException(404, 'Application not found')
    return app


async def resolve_cv(cv_id: str | None, user: User) -> PydanticObjectId | None:
    """A linked CV must exist and belong to the caller; '' / None unlinks."""
    if not cv_id:
        return None
    try:
        oid = PydanticObjectId(cv_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(422, 'Linked CV not found')
    if not await SavedCv.find_one(SavedCv.id == oid, SavedCv.user_id == user.id, ignore_cache=True).exists():
        raise HTTPException(422, 'Linked CV not found')
    return oid


@router.post('/applications', status_code=201)
async def create_application(data: ApplicationCreate, user: CurrentUser):
    cv_oid = await resolve_cv(data.cvId, user)
    app = Application(
        user_id=user.id,
        company=data.company,
        job_title=data.jobTitle,
        job_url=data.jobUrl,
        location=data.location,
        work_arrangement=data.workArrangement,
        employment_type=data.employmentType,
        status=data.status,
        date_applied=data.dateApplied,
        notes=data.notes,
        cv_id=cv_oid,
        follow_up_date=data.followUpDate,
    )
    await app.insert()
    return await present_full(app, user)


_SORT_FIELDS = {
    'createdAt': 'created_at',
    'updatedAt': 'updated_at',
    'dateApplied': 'date_applied',
    'company': 'company',
}


@router.get('/applications')
async def list_applications(
    user: CurrentUser,
    status: ApplicationStatus | None = None,
    q: str | None = None,
    sort: Literal['createdAt', 'updatedAt', 'dateApplied', 'company'] = 'createdAt',
    order: Literal['asc', 'desc'] = 'desc',
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    query = Application.find(Application.user_id == user.id)
    if status is not None:
        query = query.find(Application.status == status)
    docs = await query.to_list()

    if q and q.strip():
        term = q.strip().lower()
        docs = [a for a in docs if term in a.company.lower() or term in a.job_title.lower()]

    field = _SORT_FIELDS[sort]

    def value(a: Application):
        v = getattr(a, field)
        return v.lower() if isinstance(v, str) else v

    # Sort rows that have the field, keeping missing values last in either direction.
    ranked = sorted((a for a in docs if value(a) is not None), key=value, reverse=(order == 'desc'))
    missing = [a for a in docs if value(a) is None]
    docs = ranked + missing
    return await present_many(docs[offset : offset + limit], user)


@router.get('/applications/{app_id}')
async def get_application(app_id: str, user: CurrentUser):
    return await present_full(await owned(app_id, user), user)


_FIELD_MAP = {
    'company': 'company',
    'jobTitle': 'job_title',
    'status': 'status',
    'jobUrl': 'job_url',
    'location': 'location',
    'workArrangement': 'work_arrangement',
    'employmentType': 'employment_type',
    'dateApplied': 'date_applied',
    'notes': 'notes',
    'followUpDate': 'follow_up_date',
    'cvId': 'cv_id',
}


@router.patch('/applications/{app_id}')
async def update_application(app_id: str, data: ApplicationUpdate, user: CurrentUser):
    app = await owned(app_id, user)
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        return await present_full(app, user)
    if 'cvId' in fields:
        fields['cvId'] = await resolve_cv(fields['cvId'], user)
    changes = {_FIELD_MAP[key]: value for key, value in fields.items()}
    t = datetime.now(UTC)
    changes['updated_at'] = t
    if 'dateApplied' in fields:
        changes['date_applied_provisional'] = False  # the user set the date: no longer provisional
    status_changed = 'status' in fields and fields['status'] != app.status
    if status_changed:
        # A manual decision: older Gmail emails can no longer suggest a status for this application.
        changes['status_basis_at'] = t
    # Write only the supplied fields so history and simultaneous unrelated edits survive.
    updated = await Application.find_one(
        Application.id == app.id, Application.user_id == user.id, ignore_cache=True,
    ).update({'$set': changes}, response_type=UpdateResponse.NEW_DOCUMENT)
    if updated is None:
        raise HTTPException(404, 'Application not found')
    if status_changed:
        from app.gmail.reconcile import invalidate_status_suggestion
        await ApplicationEvent(user_id=user.id, application_id=updated.id, source='manual', event_type='status_change',
                               occurred_at=t, status_from=app.status.value, status_to=updated.status.value).insert()
        await invalidate_status_suggestion(updated)
    return await present_full(updated, user)


@router.delete('/applications/{app_id}', status_code=204)
async def delete_application(app_id: str, user: CurrentUser):
    from app.gmail.reconcile import on_application_deleted
    app = await owned(app_id, user)
    await on_application_deleted(app)  # remembers Gmail evidence while connected, drops activity and reviews
    await app.delete()
