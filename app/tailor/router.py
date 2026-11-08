"""Authenticated AI CV-tailoring endpoints under /api/v1.

Generates truthful, section-level rewrite proposals grounded in the user's ORIGINAL CV.
Never modifies the original; the accepted result is saved as a new CV via POST /cvs.
"""
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
from app.services import ai_tailoring
from app.tailor.models import TailorDraft
from app.tailor.schemas import (
    TAILORING_PROMPT_VERSION,
    ChangeSection,
    CreateTailor,
    ProposedChange,
    TailoringAnalysis,
)
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['tailor'])
CurrentUser = Annotated[User, Depends(get_current_user)]
_GLOBAL = PydanticObjectId('000000000000000000000000')  # sentinel owner for the global daily counter


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --- CV sections (PII minimized, id-tagged for grounding) ------------------------

def cv_sections(content: dict[str, Any]) -> str:
    c = content or {}
    lines: list[str] = []
    if c.get('summary'):
        lines.append(f'[summary] {c["summary"]}')
    for e in c.get('experience') or []:
        for i, b in enumerate(e.get('bullets') or []):
            if b:
                lines.append(f'[experience id={e.get("id")} bullet={i}] {b}')
    for p in c.get('projects') or []:
        if p.get('description'):
            lines.append(f'[project id={p.get("id")}] {p["description"]}')
    if c.get('skills'):
        lines.append(f'[skills] {", ".join(c["skills"])}')
    return '\n'.join(lines)


def section_text(content: dict[str, Any], ch: ProposedChange) -> str | None:
    """The actual existing CV text a change references (for factual grounding)."""
    c = content or {}
    if ch.section == ChangeSection.summary:
        return (c.get('summary') or '').strip()
    if ch.section == ChangeSection.skills:
        return ', '.join(c.get('skills') or [])
    if ch.section == ChangeSection.experience:
        e = next((x for x in c.get('experience') or [] if str(x.get('id')) == str(ch.experience_id)), None)
        bullets = (e or {}).get('bullets') or []
        if e is not None and ch.bullet_index is not None and 0 <= ch.bullet_index < len(bullets):
            return (bullets[ch.bullet_index] or '').strip()
        return None
    if ch.section == ChangeSection.project:
        p = next((x for x in c.get('projects') or [] if str(x.get('id')) == str(ch.project_id)), None)
        return (p.get('description') or '').strip() if p else None
    return None


def grounded(content: dict[str, Any], ch: ProposedChange) -> bool:
    """True when the change's `original` matches the CV's real text at that section."""
    actual = section_text(content, ch)
    if not actual:
        return False
    o = ch.original.strip()
    return o == actual or o in actual or actual in o


def job_context_from_match(result: dict[str, Any]) -> str:
    lines = [result.get('overallAssessment') or result.get('summary') or '']
    for key, label in [('matched', 'already matched'), ('partiallyMatched', 'partially matched'),
                       ('missing', 'missing/unverified')]:
        for r in result.get(key) or []:
            lines.append(f'- [{label}] {r.get("requirement")} ({r.get("importance")})')
    return '\n'.join(x for x in lines if x)


# --- quota (atomic; per-user tailor cap + shared global cap) ---------------------

async def _inc(owner: PydanticObjectId, day_key: str) -> int:
    doc = await MatchUsage.get_pymongo_collection().find_one_and_update(
        {'user_id': owner, 'day': day_key}, {'$inc': {'used': 1}},
        upsert=True, return_document=pymongo.ReturnDocument.AFTER)
    return doc['used']


async def _dec(owner: PydanticObjectId, day_key: str) -> None:
    await MatchUsage.get_pymongo_collection().update_one(
        {'user_id': owner, 'day': day_key}, {'$inc': {'used': -1}})


async def reserve_quota(user_id: PydanticObjectId) -> None:
    day = datetime.now(UTC).date().isoformat()
    if await _inc(user_id, f'tailor:{day}') > settings.tailor_daily_quota:
        await _dec(user_id, f'tailor:{day}')
        raise HTTPException(429, 'Daily CV tailoring limit reached. Please try again tomorrow.')
    if await _inc(_GLOBAL, f'ai-global:{day}') > settings.ai_global_daily_quota:
        await _dec(_GLOBAL, f'ai-global:{day}')
        await _dec(user_id, f'tailor:{day}')
        raise HTTPException(503, 'The AI service is at capacity today. Please try again tomorrow.')


async def release_quota(user_id: PydanticObjectId) -> None:
    day = datetime.now(UTC).date().isoformat()
    await _dec(user_id, f'tailor:{day}')
    await _dec(_GLOBAL, f'ai-global:{day}')


# --- presentation ----------------------------------------------------------------

def _suggestion_dto(ch: ProposedChange, idx: int) -> dict[str, Any]:
    dto: dict[str, Any] = {
        'id': f's{idx}',
        'section': ch.section.value,
        'original': ch.original,
        'suggested': ch.suggested,
        'explanation': ch.explanation,
        'reviewRequired': ch.review_required,
    }
    if ch.experience_id is not None:
        dto['experienceId'] = str(ch.experience_id)
    if ch.bullet_index is not None:
        dto['bulletIndex'] = ch.bullet_index
    if ch.project_id is not None:
        dto['projectId'] = str(ch.project_id)
    if ch.job_requirement:
        dto['jobRequirement'] = ch.job_requirement
    return dto


def present(draft: TailorDraft) -> dict[str, Any]:
    return {
        'id': str(draft.id),
        'sourceCvId': str(draft.source_cv_id),
        'matchId': draft.match_id,
        'jobTitle': draft.job_title,
        'company': draft.company or '',
        'suggestions': draft.suggestions,
        'missingQualifications': draft.missing_qualifications,
        'questions': draft.clarifying_questions,
        'tokens': {'input': draft.input_tokens, 'output': draft.output_tokens},
        'estimatedCostUsd': draft.estimated_cost_usd,
        'createdAt': draft.created_at.isoformat(),
    }


async def _owned_cv(cv_id, user: User) -> SavedCv:
    try:
        oid = PydanticObjectId(cv_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(422, 'Choose a CV to tailor.')
    cv = await SavedCv.find_one(SavedCv.id == oid, SavedCv.user_id == user.id)
    if not cv:
        raise HTTPException(404, 'Original CV not found.')
    return cv


_ERRS = {
    ai_tailoring.AiError: (502, 'The AI service is unavailable. Please try again.'),
}


@router.post('/tailor', status_code=201)
async def create_tailor(data: CreateTailor, user: CurrentUser):
    # Resolve the source CV + job context — reusing an existing match when given (no re-analysis).
    match_id = None
    if data.matchId:
        try:
            moid = PydanticObjectId(data.matchId)
        except (ValueError, TypeError, InvalidId):
            raise HTTPException(404, 'Match not found.')
        match = await MatchRecord.find_one(MatchRecord.id == moid, MatchRecord.user_id == user.id)
        if not match:
            raise HTTPException(404, 'Match not found.')
        match_id = str(match.id)
        cv = await _owned_cv(match.cv_id, user)
        job_title = match.job_title
        company = match.company
        job_context = job_context_from_match(match.result)
    else:
        if not (data.cvId and data.jobTitle and data.jobDescription):
            raise HTTPException(422, 'Provide a CV and either a match or a job description.')
        job = data.jobDescription.strip()
        if not (settings.match_job_description_min_chars <= len(job) <= settings.match_job_description_max_chars):
            raise HTTPException(422, 'Job description is too short or too long.')
        cv = await _owned_cv(data.cvId, user)
        job_title = data.jobTitle.strip()
        company = data.company
        job_context = job

    sections = cv_sections(cv.content)
    if not sections.strip():
        raise HTTPException(422, 'This CV has no content to tailor yet.')
    cv_hash = _sha(sections)
    job_hash = _sha(f'{job_title.lower()}\n{job_context}')

    # Cache / dedup: identical tailoring for this user is reused (no OpenAI call, no quota).
    cutoff = datetime.now(UTC) - timedelta(hours=settings.tailor_cache_ttl_hours)
    cached = await TailorDraft.find(
        TailorDraft.user_id == user.id, TailorDraft.cv_hash == cv_hash, TailorDraft.job_hash == job_hash,
        TailorDraft.model == settings.openai_model, TailorDraft.prompt_version == TAILORING_PROMPT_VERSION,
        TailorDraft.created_at > cutoff,
    ).sort('-created_at').first_or_none()
    if cached:
        return present(cached)

    await reserve_quota(user.id)
    try:
        out = await run_in_threadpool(ai_tailoring.analyze, job_title, company, job_context, sections)
    except ai_tailoring.AiError as e:
        await release_quota(user.id)
        status, message = next(((s, m) for t, (s, m) in _ERRS.items() if isinstance(e, t)),
                               (502, 'The AI service is unavailable.'))
        # Reuse the richer mapping from matching for specific error types.
        from app.matches.router import _ERROR_STATUS
        status, message = _ERROR_STATUS.get(type(e), (status, message))
        raise HTTPException(status, message)

    analysis: TailoringAnalysis = out.analysis
    # Factual grounding: flag any change whose "original" is not actually in the CV.
    suggestions = []
    for i, ch in enumerate(analysis.changes):
        if not grounded(cv.content, ch):
            ch.review_required = True
        suggestions.append(_suggestion_dto(ch, i))

    draft = TailorDraft(
        user_id=user.id, source_cv_id=cv.id, match_id=match_id, job_title=job_title, company=company,
        cv_hash=cv_hash, job_hash=job_hash, model=out.model, prompt_version=out.prompt_version,
        suggestions=suggestions,
        missing_qualifications=analysis.missing_qualifications,
        clarifying_questions=analysis.clarifying_questions,
        input_tokens=out.usage.input_tokens, output_tokens=out.usage.output_tokens,
        estimated_cost_usd=ai_tailoring.estimate_cost_usd(out.usage),
    )
    await draft.insert()
    return present(draft)


@router.get('/tailor/{draft_id}')
async def get_tailor(draft_id: str, user: CurrentUser):
    try:
        oid = PydanticObjectId(draft_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(404, 'Draft not found.')
    draft = await TailorDraft.find_one(TailorDraft.id == oid, TailorDraft.user_id == user.id)
    if not draft:
        raise HTTPException(404, 'Draft not found.')
    return present(draft)
