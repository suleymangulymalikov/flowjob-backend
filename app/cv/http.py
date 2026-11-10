"""Bound request parsing and PDF work for the CV endpoints."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from anyio import CapacityLimiter, WouldBlock
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.cv.content import MAX_CV_BODY_BYTES, CvContent, CvWorkloadError, read_cv_content

_log = logging.getLogger(__name__)
_pdf_slots = CapacityLimiter(2)


class CvPayloadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            size = 0
            too_large = HTTPException(413, 'CV request is too large. The limit is 256 KiB.')
            length = request.headers.get('content-length')
            if length is not None:
                try:
                    size_hint = int(length)
                except ValueError:
                    raise HTTPException(400, 'Invalid Content-Length.') from None
                if size_hint > MAX_CV_BODY_BYTES:
                    raise too_large

            async def receive():
                nonlocal size
                message = await request.receive()
                size += len(message.get('body', b''))
                if size > MAX_CV_BODY_BYTES:
                    raise too_large
                return message

            try:
                return await handler(Request(request.scope, receive))
            except RequestValidationError as error:
                # Return actionable locations without echoing private CV text or arbitrary keys.
                details = [
                    {'loc': item['loc'][:-1] if item['type'] == 'extra_forbidden' else item['loc'],
                     'msg': item['msg'], 'type': item['type']}
                    for item in error.errors()[:12]
                ]
                return JSONResponse(status_code=422, content={'detail': details})

        return bounded


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
