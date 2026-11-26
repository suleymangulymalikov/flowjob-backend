"""Open jobs in Poland from tracked companies, under /api/v1/jobs (login required).

With `cvId`, every job also gets a keyword skill "fit" against that CV (see skills.py).
That score uses no AI: the AI match only runs when the user asks for it on Job Match.
"""
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user
from app.integration.models import SavedCv
from app.jobs import service
from app.jobs.models import JobPosting
from app.jobs.skills import cv_skills, fit
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


class JobListView(BaseModel):
    """What the list needs from MongoDB: everything except the (large) description."""
    id: PydanticObjectId = Field(alias='_id')
    title: str
    company: str
    city: str
    location: str
    level: str
    url: str | None = None
    vendor: str
    posted_at: datetime | None = None
    skills: dict[str, Any] | None = None


def present(p: JobPosting | JobListView) -> dict[str, Any]:
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


async def _owned_cv(cv_id: str, user: User) -> SavedCv:
    try:
        cv = await SavedCv.find_one(SavedCv.id == PydanticObjectId(cv_id), SavedCv.user_id == user.id)
    except (InvalidId, ValueError, TypeError):
        cv = None
    if not cv:
        raise HTTPException(404, 'CV not found')
    return cv


def _fit_sort_key(item: tuple[JobListView, dict]) -> tuple:
    posting, f = item
    posted = service.as_utc(posting.posted_at)
    # Scored jobs first (highest score), then unscored; newest first within the same score.
    return (f['score'] is None, -(f['score'] or 0), -(posted.timestamp() if posted else 0), posting.title)


@router.get('/jobs')
async def list_jobs(
    user: CurrentUser,
    level: Literal['entry', 'internship', 'all'] = 'entry',
    city: Annotated[str | None, Query(max_length=60)] = None,
    company: Annotated[str | None, Query(max_length=120)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    cv_id: Annotated[str | None, Query(alias='cvId', max_length=40)] = None,
    sort: Literal['recent', 'fit'] = 'recent',
    page: Annotated[int, Query(ge=1, le=500)] = 1,
    page_size: Annotated[int, Query(alias='pageSize', ge=1, le=50)] = 20,
) -> dict[str, Any]:
    if sort == 'fit' and not cv_id:
        raise HTTPException(422, 'Choose a CV to sort by fit')
    cv = cv_skills((await _owned_cv(cv_id, user)).content) if cv_id else None
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

    query = JobPosting.find(match).project(JobListView)
    total = await JobPosting.find(match).count()
    start = (page - 1) * page_size
    if sort == 'fit':
        # Scores depend on the CV, so rank every matching job here (a few hundred at most), then page.
        everything = await query.to_list()
        ranked = sorted(((p, fit(p.skills, cv)) for p in everything), key=_fit_sort_key)
        rows = ranked[start:start + page_size]
    else:
        page_items = await query.sort([('posted_at', -1), ('title', 1)]).skip(start).limit(page_size).to_list()
        rows = [(p, fit(p.skills, cv) if cv else None) for p in page_items]

    refreshed_at = await service.last_refreshed_at()
    return {
        'items': [{**present(p), **({'fit': f} if f is not None else {})} for p, f in rows],
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
