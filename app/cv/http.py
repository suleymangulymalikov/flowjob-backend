"""Bound request parsing and PDF work for the CV endpoints."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from anyio import CapacityLimiter, WouldBlock
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from app.cv.content import MAX_CV_BODY_BYTES, CvContent, CvWorkloadError, read_cv_content
from app.request_limits import BoundedPayloadRoute

_log = logging.getLogger(__name__)
_pdf_slots = CapacityLimiter(2)


class CvPayloadRoute(BoundedPayloadRoute):
    max_body_bytes = MAX_CV_BODY_BYTES
    body_limit_message = 'CV request is too large. The limit is 256 KiB.'


def checked_saved_content(raw: dict) -> dict:
    content = read_cv_content(raw)
    try:
        CvContent.model_validate(content)
    except ValidationError:
        raise HTTPException(422, 'This saved CV exceeds the supported limits or contains invalid text. Edit it and try again.') from None
    return content


async def render_pdf(renderer: Callable[..., bytes], *args: Any) -> bytes:
    """Admit at most two active renders per process and keep the slot until work finishes."""
    borrower = object()
    try:
        _pdf_slots.acquire_on_behalf_of_nowait(borrower)
    except WouldBlock:
        raise HTTPException(
            503, 'PDF rendering is busy. Please try again shortly.', headers={'Retry-After': '1'},
        ) from None

    async def work() -> bytes:
        try:
            return await run_in_threadpool(renderer, *args)
        except CvWorkloadError as error:
            raise HTTPException(422, str(error)) from None
        except Exception:
            _log.error('Harvard PDF rendering failed.')
            raise HTTPException(503, 'Could not generate the PDF. Please try again.') from None
        finally:
            _pdf_slots.release_on_behalf_of(borrower)

    def observe(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()  # A disconnected client may no longer be awaiting this result.

    task = asyncio.create_task(work())
    task.add_done_callback(observe)
    return await asyncio.shield(task)
