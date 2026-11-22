"""POST /api/v1/cvs/import: organize an uploaded PDF CV into the CV Builder's structure for the user to review.

Nothing is stored: the PDF, its text and the result stay in memory for this request only. The reviewed CV is saved
later, and only through POST /api/v1/cvs after the user confirms it.

Order of work, so failures never cost an AI call or a quota unit:
consent and file checks -> bounded text extraction -> reserve one `import` quota unit -> one AI call (refunded if it
fails) -> grounding checks against the extracted text -> CvContent validation.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from app.auth.dependencies import get_current_user
from app.cv.content import CvContent, CvWorkloadError
from app.cv_import.extract import ERRORS, MAX_PDF_BYTES, ImportFileError, extract_text
from app.cv_import.grounding import ground
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


@router.post('/cvs/import')
async def import_cv(
    request: Request,
    user: CurrentUser,
    consent: Annotated[str | None, Header(alias=CONSENT_HEADER)] = None,
):
    """Body: the PDF itself (`Content-Type: application/pdf`, at most 5 MB). Requires the consent header, which the
    app sends only after the user agrees that the CV text is sent to OpenAI to organize it."""
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

    async with generation_quota(user.id, 'import'):
        try:
            # The billable call runs off the event loop. Tests replace ai_cv_import.analyze.
            result = await run_in_threadpool(ai_cv_import.analyze, extracted.text)
        except AiError as error:
            status, message = error_response(error)
            raise HTTPException(status, message) from None  # leaving the block refunds the quota unit

    content, warnings, missing = ground(result.analysis, extracted.text)
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
