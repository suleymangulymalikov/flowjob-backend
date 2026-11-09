"""Open jobs in Poland from tracked companies, under /api/v1/jobs (login required)."""
import re
from typing import Annotated, Any, Literal

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import get_current_user
from app.jobs import service
from app.jobs.models import JobPosting
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['jobs'])
CurrentUser = Annotated[User, Depends(get_current_user)]

LEVELS = {
    'entry': ['internship', 'entry'],  # default tab: everything a student or graduate can apply for
    'internship': ['internship'],
    'all': ['internship', 'entry', 'other'],
}
VENDOR_NAMES = {'greenhouse': 'Greenhouse', 'lever': 'Lever', 'ashby': 'Ashby', 'smartrecruiters': 'SmartRecruiters',
                'recruitee': 'Recruitee', 'workable': 'Workable'}


def present(p: JobPosting) -> dict[str, Any]:
    return {
        'id': str(p.id),
        'title': p.title,
        'company': p.company,
        'city': p.city,
        'location': p.location,
        'level': p.level,
        'url': p.url,
        'postedAt': service.as_utc(p.posted_at).isoformat() if p.posted_at else None,
        'source': VENDOR_NAMES.get(p.vendor, p.vendor),
    }


async def _counts(match: dict, field: str) -> list[dict]:
    rows = await JobPosting.get_pymongo_collection().aggregate([
        {'$match': match}, {'$group': {'_id': f'${field}', 'count': {'$sum': 1}}}, {'$sort': {'count': -1, '_id': 1}},
    ])
    return [{'name': r['_id'], 'count': r['count']} async for r in rows]


@router.get('/jobs')
async def list_jobs(
    user: CurrentUser,
    level: Literal['entry', 'internship', 'all'] = 'entry',
    city: Annotated[str | None, Query(max_length=60)] = None,
    company: Annotated[str | None, Query(max_length=120)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1, le=500)] = 1,
    page_size: Annotated[int, Query(alias='pageSize', ge=1, le=50)] = 20,
) -> dict[str, Any]:
    refreshing = await service.ensure_fresh()

    base: dict[str, Any] = {'level': {'$in': LEVELS[level]}}
    if q and q.strip():
        pattern = re.compile(re.escape(q.strip()), re.I)
        base['$or'] = [{'title': pattern}, {'company': pattern}]
    match = dict(base)
    if city:
        match['city'] = city
    if company:
        match['company'] = company

    total = await JobPosting.find(match).count()
    items = await (JobPosting.find(match).sort([('posted_at', -1), ('title', 1)])
                   .skip((page - 1) * page_size).limit(page_size).to_list())
    refreshed_at = await service.last_refreshed_at()
    return {
        'items': [present(p) for p in items],
        'total': total,
        'page': page,
        'pageSize': page_size,
        # Counts for the filter controls. Each one ignores its own filter so every option stays visible.
        'levels': {r['name']: r['count'] for r in await _counts({k: v for k, v in base.items() if k != 'level'}, 'level')},
        'cities': await _counts({**base, **({'company': company} if company else {})}, 'city'),
        'companies': await _counts({**base, **({'city': city} if city else {})}, 'company'),
        'refreshing': refreshing,
        'refreshedAt': refreshed_at.isoformat() if refreshed_at else None,
    }


@router.get('/jobs/{job_id}')
async def get_job(job_id: str, user: CurrentUser) -> dict[str, Any]:
    try:
        posting = await JobPosting.get(PydanticObjectId(job_id))
    except (InvalidId, ValueError, TypeError):
        posting = None
    if not posting:
        raise HTTPException(404, 'Job not found')
    description = await service.load_description(posting)
    return {**present(posting), 'description': description}
