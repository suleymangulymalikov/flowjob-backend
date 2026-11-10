"""Authenticated AI job-matching endpoints under /api/v1."""
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.auth.dependencies import get_current_user
from app.config import settings
from app.cv.http import checked_saved_content
from app.cv.revision import content_revision
from app.integration.models import SavedCv
from app.matches.models import MatchRecord
from app.matches.schemas import (
    ANALYSIS_VERSION,
    PROMPT_VERSION,
    AiAnalysis,
    CreateMatch,
    EvidenceStatus,
)
from app.matches.scoring import compute_score, explain, verdict_for
from app.services import ai_matching
from app.services.ai_cache import cache_model, coalesce_request, job_fingerprint
from app.services.ai_provider import AiError, error_response, estimate_cost_usd
from app.services.ai_quota import generation_quota
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


# --- CV ownership ----------------------------------------------------------------

async def owned_cv(cv_id: str, user: User) -> SavedCv:
    try:
        oid = PydanticObjectId(cv_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(422, 'Choose a CV to compare.')
    cv = await SavedCv.find_one(SavedCv.id == oid, SavedCv.user_id == user.id, ignore_cache=True)
    if not cv:
        raise HTTPException(422, 'Choose a CV to compare.')
    return cv


@router.post('/matches', status_code=201)
async def create_match(data: CreateMatch, user: CurrentUser):
    job = data.jobDescription.strip()
    if len(job) < settings.match_job_description_min_chars:
        raise HTTPException(422, 'Paste a bit more of the job post so we can compare it.')
    if len(job) > settings.match_job_description_max_chars:
        raise HTTPException(422, 'That job description is too long; please shorten it.')

    cv = await owned_cv(data.cvId, user)
    evidence = cv_evidence(checked_saved_content(cv.content))
    cv_hash = _sha(evidence)
    revision = content_revision(cv.content)
    job_title = data.jobTitle.strip()
    job_hash = job_fingerprint(job_title, data.company, data.jobUrl, job)
    model = cache_model()

    async def lookup() -> MatchRecord | None:
        # Look up reusable analyses in MongoDB so a deleted report cannot be reused.
        cutoff = datetime.now(UTC) - timedelta(hours=settings.match_cache_ttl_hours)
        return await MatchRecord.find(
            MatchRecord.user_id == user.id,
            MatchRecord.cv_id == cv.id,
            MatchRecord.cv_name == cv.name,
            MatchRecord.cv_revision == revision,
            MatchRecord.cv_hash == cv_hash,
            MatchRecord.job_hash == job_hash,
            MatchRecord.model == model,
            MatchRecord.prompt_version == PROMPT_VERSION,
            MatchRecord.created_at > cutoff,
            ignore_cache=True,
        ).sort('-created_at').first_or_none()

    async def generate() -> MatchRecord:
        async with generation_quota(user.id, 'match'):
            try:
                # Billable call runs off the event loop. Tests monkeypatch ai_matching.analyze.
                out = await run_in_threadpool(
                    ai_matching.analyze, job_title, data.company, job, evidence
                )
            except AiError as error:
                status, message = error_response(error)
                raise HTTPException(status, message) from error

            score = compute_score(out.analysis)
            result = build_result(out.analysis, score, out.model)
            record = MatchRecord(
                user_id=user.id,
                cv_id=cv.id,
                cv_name=cv.name,
                job_title=job_title,
                company=data.company,
                job_url=data.jobUrl,
                cv_hash=cv_hash,
                cv_revision=revision,
                job_hash=job_hash,
                model=out.model,
                prompt_version=out.prompt_version,
                result=result,
                score=score,
                input_tokens=out.usage.input_tokens,
                output_tokens=out.usage.output_tokens,
                estimated_cost_usd=estimate_cost_usd(out.usage),
            )
            await record.insert()
        return record

    identity = ('match', user.id, cv.id, cv.name, revision, job_hash, model, PROMPT_VERSION)
    return present(await coalesce_request(identity, lookup, generate))


@router.get('/matches')
async def list_matches(user: CurrentUser):
    records = await MatchRecord.find(MatchRecord.user_id == user.id).sort('-created_at').to_list()
    return [present(r) for r in records]


def owned_match_query(match_id: str, user: User):
    try:
        oid = PydanticObjectId(match_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(404, 'Match not found')
    return MatchRecord.find_one(MatchRecord.id == oid, MatchRecord.user_id == user.id, ignore_cache=True)


@router.get('/matches/{match_id}')
async def get_match(match_id: str, user: CurrentUser):
    record = await owned_match_query(match_id, user)
    if not record:
        raise HTTPException(404, 'Match not found')
    return present(record)


@router.delete('/matches/{match_id}', status_code=204)
async def delete_match(match_id: str, user: CurrentUser):
    # Apply the owner filter in the delete itself. Other records keep their history and provenance.
    result = await owned_match_query(match_id, user).delete()
    if result is None or result.deleted_count == 0:
        raise HTTPException(404, 'Match not found')
