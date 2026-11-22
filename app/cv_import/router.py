"""POST /api/v1/profile/import/pdf: organize an uploaded PDF CV into the Profile's structure for the user to review.

Nothing is stored: the PDF, its text and the result stay in memory for this request only. The reviewed result is
saved later, and only through POST /api/v1/profile/import (profile.py) after the user confirms it, once per account.

Order of work, so failures never cost an AI call or a quota unit:
import not used yet -> consent and file checks -> bounded text extraction -> reserve one `import` quota unit -> one
AI call (refunded if it fails) -> grounding checks against the extracted text -> CvContent validation.

The quota reservation and the AI call run in their own task, shielded from the request (the pattern Match and Tailor
use via coalesce_request): if the client disconnects or the request is cancelled, the billable call still settles its
quota unit instead of refunding it.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from app.auth.dependencies import get_current_user
from app.cv.content import CvContent, CvWorkloadError
from app.cv_import.extract import ERRORS, MAX_PDF_BYTES, ImportFileError, extract_text
from app.cv_import.grounding import ground
from app.cv_import.profile import ALREADY_IMPORTED, import_used
from app.request_limits import BoundedPayloadRoute
from app.services import ai_cv_import
from app.services.ai_provider import AiError, error_response
from app.services.ai_quota import generation_quota
from app.users.models import User

_log = logging.getLogger(__name__)

CONSENT_HEADER = 'X-FlowJob-AI-Consent'
CONSENT_VALUE = 'cv-import'


class ImportPayloadRoute(BoundedPayloadRoute):
    max_body_bytes = MAX_PDF_BYTES
    body_limit_message = ERRORS['too_large'][1]
    no_store = True  # the response holds personal data from the CV


router = APIRouter(prefix='/api/v1', tags=['cv-import'], route_class=ImportPayloadRoute)
CurrentUser = Annotated[User, Depends(get_current_user)]


async def _organize(user: User, text: str, layout_text: str | None) -> ai_cv_import.ImportResult:
    """Reserve one quota unit and make the AI call in a task that outlives a cancelled request."""

    async def generate() -> ai_cv_import.ImportResult:
        async with generation_quota(user.id, 'import'):
            try:
                # The billable call runs off the event loop. Tests replace ai_cv_import.analyze.
                return await run_in_threadpool(ai_cv_import.analyze, text, layout_text)
            except AiError as error:
                status, message = error_response(error)
                raise HTTPException(status, message) from None  # leaving the block refunds the quota unit

    def observe(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()  # a disconnected client may no longer be awaiting this result

    task = asyncio.create_task(generate())
    task.add_done_callback(observe)
    return await asyncio.shield(task)


@router.post('/profile/import/pdf')
async def import_cv(
    request: Request,
    user: CurrentUser,
    consent: Annotated[str | None, Header(alias=CONSENT_HEADER)] = None,
):
    """Body: the PDF itself (`Content-Type: application/pdf`, at most 5 MB). Requires the consent header, which the
    app sends only after the user agrees that the CV text is sent to OpenAI to organize it."""
    if await import_used(user):
        raise HTTPException(*ALREADY_IMPORTED)  # before any work, so it never costs an AI call or a quota unit
    if consent != CONSENT_VALUE:
        raise HTTPException(428, 'Confirm that your CV text may be sent to OpenAI to organize it, then try again.')
    media_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if media_type != 'application/pdf':
        raise HTTPException(*ERRORS['not_pdf'])

    try:
        extracted = await extract_text(await request.body())
    except ImportFileError as error:
        _log.info('CV import rejected the file: %s', error.code)  # a fixed code, never file content
        raise HTTPException(error.status, error.message) from None

    result = await _organize(user, extracted.text, extracted.layout_text)

    content, warnings, missing = ground(result.analysis, extracted.text, extracted.layout_text)
    try:
        validated = CvContent.model_validate(content).model_dump()
    except (ValidationError, CvWorkloadError):
        raise HTTPException(422, 'This CV is larger than FlowJob supports. Create it step by step instead.') from None
    return {
        'content': validated,
        'warnings': warnings,
        'missing': missing,
        'source': {'pages': extracted.pages, 'method': extracted.method},
        'model': result.model,
    }
