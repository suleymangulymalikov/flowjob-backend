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
from app.experiment.matching import build_report, render_cv
from app.matches.models import MatchRecord
from app.matches.schemas import CreateMatch
from app.services import ai_matching
from app.services.ai_matching import PROMPT_VERSION
from app.services.ai_cache import cache_model, coalesce_request, job_fingerprint
from app.services.ai_provider import AiError, error_response, estimate_cost_usd
from app.services.ai_quota import generation_quota
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['matches'])
CurrentUser = Annotated[User, Depends(get_current_user)]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --- presentation ----------------------------------------------------------------

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
    content = checked_saved_content(cv.content)
    cv_hash = _sha(render_cv(content))  # the PII-free, path-tagged rendering the model sees
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
                    ai_matching.analyze, job_title, data.company, job, content
                )
            except AiError as error:
                status, message = error_response(error)
                raise HTTPException(status, message) from error

            # The backend checks every job quote and CV citation and computes the score itself.
            result = build_report(out.analysis, content, job, out.model)
            score = result['score']
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
