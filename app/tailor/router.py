"""Authenticated AI CV-tailoring endpoints under /api/v1.

Generates truthful, section-level rewrite proposals grounded in the user's ORIGINAL CV.
Never modifies the original; the accepted result is saved as a new CV via POST /cvs.
"""
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.auth.dependencies import get_current_user
from app.config import settings
from app.cv.revision import content_revision
from app.integration.models import SavedCv
from app.matches.models import MatchRecord
from app.services import ai_tailoring
from app.services.ai_cache import cache_model, coalesce_request, job_fingerprint
from app.services.ai_provider import AiError, error_response, estimate_cost_usd
from app.services.ai_quota import generation_quota
from app.tailor.drafts import owned_draft
from app.tailor.grounding import safe_to_auto_apply, target_text
from app.tailor.models import TailorDraft
from app.tailor.schemas import (
    TAILORING_PROMPT_VERSION,
    CreateTailor,
    ProposedChange,
    TailorDraftResponse,
    TailoringAnalysis,
    VerifiedTailorDraftResponse,
)
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['tailor'])
CurrentUser = Annotated[User, Depends(get_current_user)]


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


def job_context_from_match(result: dict[str, Any]) -> str:
    lines = [result.get('overallAssessment') or result.get('summary') or '']
    for key, label in [('matched', 'already matched'), ('partiallyMatched', 'partially matched'),
                       ('missing', 'missing/unverified')]:
        for r in result.get(key) or []:
            lines.append(f'- [{label}] {r.get("requirement")} ({r.get("importance")})')
    return '\n'.join(x for x in lines if x)


# --- presentation ----------------------------------------------------------------

def _suggestion_dto(ch: ProposedChange, idx: int, original: str, review_required: bool) -> dict[str, Any]:
    dto: dict[str, Any] = {
        'id': f's{idx}',
        'section': ch.section.value,
        'original': original,
        'suggested': ch.suggested,
        'explanation': ch.explanation,
        'reviewRequired': review_required,
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
        'sourceCvRevision': draft.source_cv_revision,
        'matchId': draft.match_id,
        'jobTitle': draft.job_title,
        'company': draft.company or '',
        'jobUrl': draft.job_url,
        'suggestions': draft.suggestions if draft.source_cv_revision else [
            {**suggestion, 'reviewRequired': True} for suggestion in draft.suggestions
        ],
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
    cv = await SavedCv.find_one(SavedCv.id == oid, SavedCv.user_id == user.id, ignore_cache=True)
    if not cv:
        raise HTTPException(404, 'Original CV not found.')
    return cv


@router.get('/tailor/capabilities')
async def tailoring_capabilities() -> dict[str, int]:
    # The UI checks compatibility before making a potentially billable request.
    return {'sourceVerification': 1}


@router.post('/tailor', status_code=201, response_model=VerifiedTailorDraftResponse)
async def create_tailor(data: CreateTailor, user: CurrentUser):
    # Resolve the source CV + job context — reusing an existing match when given (no re-analysis).
    match_id = None
    if data.matchId:
        try:
            moid = PydanticObjectId(data.matchId)
        except (ValueError, TypeError, InvalidId):
            raise HTTPException(404, 'Match not found.')
        match = await MatchRecord.find_one(
            MatchRecord.id == moid, MatchRecord.user_id == user.id, ignore_cache=True,
        )
        if not match:
            raise HTTPException(404, 'Match not found.')
        match_id = str(match.id)
        cv = await _owned_cv(match.cv_id, user)
        if data.cvId is not None and data.cvId != str(cv.id):
            raise HTTPException(422, 'This match belongs to a different CV. Start a draft from the job form.')
        job_title = match.job_title
        company = match.company
        job_url = match.job_url
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
        job_url = data.jobUrl
        job_context = job

    sections = cv_sections(cv.content)
    if not sections.strip():
        raise HTTPException(422, 'This CV has no content to tailor yet.')
    cv_hash = _sha(sections)
    revision = content_revision(cv.content)
    job_hash = job_fingerprint(job_title, company, job_url, job_context)
    model = cache_model()

    async def lookup() -> TailorDraft | None:
        cutoff = datetime.now(UTC) - timedelta(hours=settings.tailor_cache_ttl_hours)
        return await TailorDraft.find(
            TailorDraft.user_id == user.id, TailorDraft.cv_hash == cv_hash, TailorDraft.job_hash == job_hash,
            TailorDraft.source_cv_id == cv.id, TailorDraft.source_cv_revision == revision,
            TailorDraft.match_id == match_id,
            TailorDraft.model == model, TailorDraft.prompt_version == TAILORING_PROMPT_VERSION,
            TailorDraft.created_at > cutoff, ignore_cache=True,
        ).sort('-created_at').first_or_none()

    async def generate() -> TailorDraft:
        async with generation_quota(user.id, 'tailor'):
            try:
                out = await run_in_threadpool(ai_tailoring.analyze, job_title, company, job_context, sections)
            except AiError as error:
                status, message = error_response(error)
                raise HTTPException(status, message) from error

            analysis: TailoringAnalysis = out.analysis
            # Bind the exact target text, and accept at most one proposal for each target.
            suggestions = []
            seen_targets = set()
            for i, change in enumerate(analysis.changes):
                original = target_text(cv.content, change)
                target = (change.section, change.experience_id, change.bullet_index, change.project_id)
                if not original or not original.strip() or change.original.strip() != original.strip():
                    continue
                if not change.suggested.strip() or target in seen_targets:
                    continue
                seen_targets.add(target)
                review_required = change.review_required or not safe_to_auto_apply(change, original, cv.content)
                suggestions.append(_suggestion_dto(change, i, original, review_required))

            draft = TailorDraft(
                user_id=user.id, source_cv_id=cv.id, source_cv_revision=revision,
                match_id=match_id, job_title=job_title, company=company, job_url=job_url,
                cv_hash=cv_hash, job_hash=job_hash, model=out.model, prompt_version=out.prompt_version,
                suggestions=suggestions,
                missing_qualifications=analysis.missing_qualifications,
                clarifying_questions=analysis.clarifying_questions,
                input_tokens=out.usage.input_tokens, output_tokens=out.usage.output_tokens,
                estimated_cost_usd=estimate_cost_usd(out.usage),
            )
            await draft.insert()
        return draft

    identity = ('tailor', user.id, cv.id, revision, match_id, job_hash, model, TAILORING_PROMPT_VERSION)
    return present(await coalesce_request(identity, lookup, generate, fresh=data.fresh))


@router.get('/tailor/{draft_id}', response_model=TailorDraftResponse)
async def get_tailor(draft_id: str, user: CurrentUser):
    return present(await owned_draft(draft_id, user))
