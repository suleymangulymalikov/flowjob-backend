"""Authenticated AI CV-tailoring endpoints under /api/v1.

Generates truthful, section-level rewrite proposals grounded in the user's ORIGINAL CV.
Never modifies the original; the accepted result is saved as a new CV via POST /cvs.
"""
import hashlib
from collections import Counter
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
from app.services import ai_tailoring
from app.services.ai_cache import cache_model, coalesce_request, job_fingerprint
from app.services.ai_provider import AiError, error_response, estimate_cost_usd
from app.services.ai_quota import generation_quota
from app.tailor.drafts import owned_draft
from app.ai_matching.matching import render_cv
from app.tailor.grounding import MAX_ADDED_SKILLS, ground
from app.tailor.models import TailorDraft
from app.tailor.schemas import (
    TAILORING_PROMPT_VERSION,
    CreateTailor,
    TailorDraftResponse,
    TailoringAnalysis,
    VerifiedTailorDraftResponse,
)
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['tailor'])
CurrentUser = Annotated[User, Depends(get_current_user)]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --- job context from a match report ---------------------------------------------

MAX_CONTEXT_CHARS = 3500


def job_context_from_match(result: dict[str, Any]) -> str:
    """Compact job context for tailoring.

    AI-first reports contribute each qualification's category, verified status and the CV quotation that shows it,
    plus the role's duties as context. Historical reports (no `requirements`) fall back to the saved assessment and
    requirement names.
    """
    lines = [result.get('overallAssessment') or result.get('summary') or '']
    detailed = result.get('requirements')
    if isinstance(detailed, list) and detailed:
        lines.append('Qualifications (importance, whether the CV shows it, CV evidence):')
        for r in detailed:
            line = f'- [{r.get("category")} | {str(r.get("status", "")).replace("_", " ")}] {r.get("requirement")}'
            cites = r.get('evidence') or []
            if cites:
                line += f' — evidence {cites[0].get("path")}: "{str(cites[0].get("quote", ""))[:110]}"'
            lines.append(line)
        duties = [e.get('text') for e in result.get('excluded') or [] if e.get('reason') == 'job_duty'][:5]
        if duties:
            lines.append('Role duties (context only, not qualifications):')
            lines += [f'- {d}' for d in duties]
    else:
        for key, label in [('matched', 'already matched'), ('partiallyMatched', 'partially matched'),
                           ('missing', 'not shown in CV')]:
            for r in result.get(key) or []:
                tag = f' ({r["importance"]})' if r.get('importance') else ''
                lines.append(f'- [{label}] {r.get("requirement")}{tag}')
    return '\n'.join(x for x in lines if x)[:MAX_CONTEXT_CHARS]


def missing_from_match(result: dict[str, Any]) -> list[str]:
    """Qualifications the match report did not fully verify, taken straight from the report (no AI involved)."""
    detailed = result.get('requirements')
    if isinstance(detailed, list) and detailed:
        names = [r.get('requirement') for r in detailed if r.get('status') != 'demonstrated']
    else:  # historical report
        names = [r.get('requirement') for r in (result.get('partiallyMatched') or []) + (result.get('missing') or [])]
    return [n for n in names if isinstance(n, str) and n][:12]


# --- presentation ----------------------------------------------------------------

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
        'proposalStats': draft.proposal_stats,
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
        detailed = match.result.get('requirements')
        if isinstance(detailed, list) and len(detailed) >= 2 and all(r.get('status') == 'not_shown' for r in detailed):
            # Nothing the post asks for is in the CV: rewording cannot honestly close that gap, and a model asked to
            # try starts inventing. Saying so is the useful answer.
            raise HTTPException(422, 'Your CV does not show any of the qualifications this job asks for yet, so '
                                     'tailoring cannot help here. Add your relevant experience to your CV first.')
        job_context = job_context_from_match(match.result)
        missing_qualifications = missing_from_match(match.result)
        # The match never stores the post. When the client still has it and it is provably the SAME post this
        # match analysed, the model also reads the employer's own wording (the single biggest quality lever).
        post = (data.jobDescription or '').strip()
        if post and len(post) <= settings.match_job_description_max_chars \
                and job_fingerprint(match.job_title, match.company, match.job_url, post) == match.job_hash:
            job_context = f'{job_context}\n\nThe job post, in the employer\'s own words:\n{post}'
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
        # No verified evidence analysis exists for a direct draft, so no "not evidenced" claims are made.
        missing_qualifications = []

    content = checked_saved_content(cv.content)
    sections = render_cv(content)  # PII-free, every item tagged with its path
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
            # Resolve every proposal against the source CV. Anything invalid is COUNTED, never silently lost.
            suggestions: list[dict[str, Any]] = []
            rejected: Counter[str] = Counter()
            rejected_examples: list[dict[str, str]] = []  # op, path and reason only: never CV or job text
            seen_targets: set[tuple[str, str]] = set()
            for i, change in enumerate(analysis.changes):
                fields, reason = ground(content, change)
                target = (change.op if change.op == 'add_skill' else 'edit', change.path)
                if fields is not None and change.op != 'add_skill' and target in seen_targets:
                    fields, reason = None, 'duplicate_target'
                if fields is not None and change.op == 'add_skill' and sum(
                        1 for x in suggestions if x['op'] == 'add_skill') >= MAX_ADDED_SKILLS:
                    fields, reason = None, 'too_many_added_skills'
                if fields is None:
                    rejected[reason or 'invalid'] += 1
                    rejected_examples.append({'op': change.op, 'path': change.path[:100], 'reason': reason or 'invalid'})
                    continue
                seen_targets.add(target)
                suggestions.append({'id': f's{i}', **fields, 'reviewRequired': fields['risk'] != 'auto'})
            stats = {'proposed': len(analysis.changes), 'kept': len(suggestions), 'rejected': dict(rejected),
                     'rejectedExamples': rejected_examples[:12]}

            draft = TailorDraft(
                user_id=user.id, source_cv_id=cv.id, source_cv_revision=revision,
                match_id=match_id, job_title=job_title, company=company, job_url=job_url,
                cv_hash=cv_hash, job_hash=job_hash, model=out.model, prompt_version=out.prompt_version,
                suggestions=suggestions, proposal_stats=stats,
                missing_qualifications=missing_qualifications,
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
