"""Authenticated AI job-matching endpoints under /api/v1."""
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import pymongo
from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.auth.dependencies import get_current_user
from app.config import settings
from app.integration.models import SavedCv
from app.matches.models import MatchRecord, MatchUsage
from app.matches.schemas import (
    ANALYSIS_VERSION,
    PROMPT_VERSION,
    AiAnalysis,
    CreateMatch,
    EvidenceStatus,
)
from app.matches.scoring import compute_score, explain, verdict_for
from app.services import ai_matching
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['matches'])
CurrentUser = Annotated[User, Depends(get_current_user)]


# --- CV evidence (PII minimized) -------------------------------------------------

def cv_evidence(content: dict[str, Any]) -> str:
    """Relevant CV content for matching, excluding name, email, phone, location and links."""
    c = content or {}
    lines: list[str] = []
    if c.get('summary'):
        lines.append(f'Summary: {c["summary"]}')
    if c.get('experience'):
        lines.append('Experience:')
        for e in c['experience']:
            dates = ' '.join(x for x in [e.get('start', ''), e.get('end', '')] if x)
            head = f'- {e.get("title", "")} at {e.get("company", "")}'
            lines.append(head + (f' ({dates})' if dates else ''))
            for b in e.get('bullets') or []:
                if b:
                    lines.append(f'    • {b}')
    if c.get('education'):
        lines.append('Education:')
        for e in c['education']:
            note = f' — {e.get("note")}' if e.get('note') else ''
            lines.append(f'- {e.get("degree", "")}, {e.get("school", "")}{note}')
    if c.get('projects'):
        lines.append('Projects:')
        for p in c['projects']:
            tech = f' [{p.get("tech")}]' if p.get('tech') else ''
            desc = f': {p.get("description")}' if p.get('description') else ''
            lines.append(f'- {p.get("name", "")}{tech}{desc}')
    if c.get('skills'):
        lines.append('Skills: ' + ', '.join(c['skills']))
    if c.get('languages'):
        lines.append(f'Languages: {c["languages"]}')
    return '\n'.join(lines).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --- presentation ----------------------------------------------------------------

def _req_dto(r) -> dict[str, Any]:
    return {
        'requirement': r.requirement,
        'importance': r.importance.value,
        'status': r.status.value,
        'evidence': r.evidence,
    }


def build_result(analysis: AiAnalysis, score: int | None, model: str) -> dict[str, Any]:
    reqs = analysis.requirements
    matched = [_req_dto(r) for r in reqs if r.status == EvidenceStatus.clearly_demonstrated]
    partial = [_req_dto(r) for r in reqs if r.status == EvidenceStatus.partially_demonstrated]
    missing = [
        _req_dto(r)
        for r in reqs
        if r.status in (EvidenceStatus.not_mentioned, EvidenceStatus.uncertain)
    ]
    return {
        'scoreAvailable': score is not None,
        'verdict': verdict_for(score),
        'summary': analysis.overall_assessment,  # legacy field
        'overallAssessment': analysis.overall_assessment,
        'scoreExplanation': explain(analysis, score),
        'matched': matched,
        'partiallyMatched': partial,
        'missing': missing,
        # Legacy compatibility for the existing UI/demo shapes.
        'strengths': [{'skill': r['requirement'], 'evidence': r['evidence']} for r in matched],
        'gaps': [{'skill': r['requirement'], 'tip': r['evidence']} for r in missing],
        'areasToImprove': analysis.areas_to_improve,
        'recommendations': analysis.recommendations,
        'suggestedWording': [
            {'section': s.section, 'suggestion': s.suggestion} for s in analysis.suggested_wording
        ],
        'questions': analysis.clarifying_questions,
        'model': model,
        'analysisVersion': ANALYSIS_VERSION,
    }


def present(record: MatchRecord) -> dict[str, Any]:
    return {
        'id': str(record.id),
        'cvId': str(record.cv_id),
        'cvName': record.cv_name,
        'jobTitle': record.job_title,
        'company': record.company or '',
        'jobUrl': record.job_url,
        'jobText': '',  # raw job text is not stored (privacy)
        'score': record.score if record.score is not None else 0,
        'createdAt': record.created_at.isoformat(),
        'tokens': {'input': record.input_tokens, 'output': record.output_tokens},
        'estimatedCostUsd': record.estimated_cost_usd,
        **record.result,
    }


# --- quota (atomic) --------------------------------------------------------------

async def reserve_quota(user_id: PydanticObjectId) -> None:
    day = datetime.now(UTC).date().isoformat()
    coll = MatchUsage.get_pymongo_collection()
    doc = await coll.find_one_and_update(
        {'user_id': user_id, 'day': day},
        {'$inc': {'used': 1}},
        upsert=True,
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if doc['used'] > settings.match_daily_quota:
        await coll.update_one({'user_id': user_id, 'day': day}, {'$inc': {'used': -1}})
        raise HTTPException(429, 'Daily AI match limit reached. Please try again tomorrow.')


async def release_quota(user_id: PydanticObjectId) -> None:
    day = datetime.now(UTC).date().isoformat()
    await MatchUsage.get_pymongo_collection().update_one(
        {'user_id': user_id, 'day': day}, {'$inc': {'used': -1}}
    )


# --- CV ownership ----------------------------------------------------------------

async def owned_cv(cv_id: str, user: User) -> SavedCv:
    try:
        oid = PydanticObjectId(cv_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(422, 'Choose a CV to compare.')
    cv = await SavedCv.find_one(SavedCv.id == oid, SavedCv.user_id == user.id)
    if not cv:
        raise HTTPException(422, 'Choose a CV to compare.')
    return cv


_ERROR_STATUS = {
    ai_matching.AiNotConfigured: (503, 'AI matching is not configured yet.'),
    ai_matching.AiTimeout: (504, 'The analysis timed out. Please try again.'),
    ai_matching.AiRateLimited: (429, 'The AI service is busy. Please try again shortly.'),
    ai_matching.AiInvalidResponse: (502, 'The AI service returned an unusable result.'),
    ai_matching.AiProviderError: (502, 'The AI service is unavailable. Please try again.'),
}


@router.post('/matches', status_code=201)
async def create_match(data: CreateMatch, user: CurrentUser):
    job = data.jobDescription.strip()
    if len(job) < settings.match_job_description_min_chars:
        raise HTTPException(422, 'Paste a bit more of the job post so we can compare it.')
    if len(job) > settings.match_job_description_max_chars:
        raise HTTPException(422, 'That job description is too long; please shorten it.')

    cv = await owned_cv(data.cvId, user)
    evidence = cv_evidence(cv.content)
    cv_hash = _sha(evidence)
    job_hash = _sha(f'{data.jobTitle.strip().lower()}\n{job}')

    # Cache / dedup: an identical analysis for this user is reused (no OpenAI call, no quota).
    cutoff = datetime.now(UTC) - timedelta(hours=settings.match_cache_ttl_hours)
    cached = await MatchRecord.find(
        MatchRecord.user_id == user.id,
        MatchRecord.cv_hash == cv_hash,
        MatchRecord.job_hash == job_hash,
        MatchRecord.model == settings.openai_model,
        MatchRecord.prompt_version == PROMPT_VERSION,
        MatchRecord.created_at > cutoff,
    ).sort('-created_at').first_or_none()
    if cached:
        return present(cached)

    await reserve_quota(user.id)
    try:
        # Billable call runs off the event loop. Tests monkeypatch ai_matching.analyze.
        out = await run_in_threadpool(
            ai_matching.analyze, data.jobTitle.strip(), data.company, job, evidence
        )
    except ai_matching.AiError as e:
        await release_quota(user.id)  # a failed analysis should not consume the user's quota
        status, message = _ERROR_STATUS.get(type(e), (502, 'The AI service is unavailable.'))
        raise HTTPException(status, message)

    score = compute_score(out.analysis)
    result = build_result(out.analysis, score, out.model)
    record = MatchRecord(
        user_id=user.id,
        cv_id=cv.id,
        cv_name=cv.name,
        job_title=data.jobTitle.strip(),
        company=data.company,
        job_url=data.jobUrl,
        cv_hash=cv_hash,
        job_hash=job_hash,
        model=out.model,
        prompt_version=out.prompt_version,
        result=result,
        score=score,
        input_tokens=out.usage.input_tokens,
        output_tokens=out.usage.output_tokens,
        estimated_cost_usd=ai_matching.estimate_cost_usd(out.usage),
    )
    await record.insert()
    return present(record)


@router.get('/matches')
async def list_matches(user: CurrentUser):
    records = await MatchRecord.find(MatchRecord.user_id == user.id).sort('-created_at').to_list()
    return [present(r) for r in records]


@router.get('/matches/{match_id}')
async def get_match(match_id: str, user: CurrentUser):
    try:
        oid = PydanticObjectId(match_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(404, 'Match not found')
    record = await MatchRecord.find_one(MatchRecord.id == oid, MatchRecord.user_id == user.id)
    if not record:
        raise HTTPException(404, 'Match not found')
    return present(record)
