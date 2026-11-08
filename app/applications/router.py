"""Authenticated, owner-scoped job application endpoints under /api/v1."""
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.applications.models import Application, ApplicationStatus
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
        # Gmail/timeline features are not implemented yet; the UI expects these keys.
        'history': [],
        'suggestion': None,
    }


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
    if not await SavedCv.find_one(SavedCv.id == oid, SavedCv.user_id == user.id):
        raise HTTPException(422, 'Linked CV not found')
    return oid


@router.post('/applications', status_code=201)
async def create_application(data: ApplicationCreate, user: CurrentUser):
    cv_oid = await resolve_cv(data.cvId, user)
    app = Application(
        user_id=user.id,
        company=data.company.strip(),
        job_title=data.jobTitle.strip(),
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
    return present(app)


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
    return [present(a) for a in docs[offset : offset + limit]]


@router.get('/applications/{app_id}')
async def get_application(app_id: str, user: CurrentUser):
    return present(await owned(app_id, user))


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
}


@router.patch('/applications/{app_id}')
async def update_application(app_id: str, data: ApplicationUpdate, user: CurrentUser):
    app = await owned(app_id, user)
    fields = data.model_dump(exclude_unset=True)
    if 'cvId' in fields:
        app.cv_id = await resolve_cv(fields.pop('cvId'), user)
    for key, attr in _FIELD_MAP.items():
        if key in fields:
            value = fields[key]
            if isinstance(value, str) and key in ('company', 'jobTitle'):
                value = value.strip()
            setattr(app, attr, value)
    app.updated_at = datetime.now(UTC)
    await app.save()
    return present(app)


@router.delete('/applications/{app_id}', status_code=204)
async def delete_application(app_id: str, user: CurrentUser):
    await (await owned(app_id, user)).delete()
