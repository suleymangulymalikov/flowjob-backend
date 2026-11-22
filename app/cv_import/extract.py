"""Bounded text extraction from an uploaded CV file. Nothing is written to disk or the database.

Today only text-based PDFs are supported (`pdf-text`, parsed with pypdf in an isolated worker process). The result
records which method produced the text, so a later OCR method can be added as a fallback for scanned PDFs (the
`no_text` case) without changing the endpoint or the grounding checks: they only need the extracted text.
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from anyio import CapacityLimiter, WouldBlock

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 10
MAX_TEXT_CHARS = 30_000
MIN_TEXT_CHARS = 40  # fewer letters or digits than this: treated as a scanned or empty PDF
WORKER_TIMEOUT_SECONDS = 15
WORKER_CPU_SECONDS = 10
WORKER_MEMORY_BYTES = 768 * 1024 * 1024
_MAX_WORKER_OUTPUT = 2 * 1024 * 1024

_WORKER = Path(__file__).with_name('worker.py')
_slots = CapacityLimiter(2)  # at most two PDFs are parsed at a time per API process
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]')

# HTTP status and user-facing message for every failure. Messages never include file content.
ERRORS: dict[str, tuple[int, str]] = {
    'empty': (422, 'The uploaded file is empty.'),
    'too_large': (413, 'The PDF is too large. The limit is 5 MB.'),
    'not_pdf': (415, 'Upload your CV as a PDF file.'),
    'encrypted': (422, 'This PDF is password-protected. Upload a version without a password.'),
    'unreadable': (422, 'This PDF could not be read. Export it again from your editor and try once more.'),
    'too_many_pages': (422, f'The PDF has too many pages. A CV can have at most {MAX_PAGES} pages.'),
    'too_much_text': (422, 'The PDF contains more text than a CV import supports.'),
    'too_complex': (422, 'This PDF is too complex to read. Export it again as a simple PDF and try once more.'),
    'no_text': (422, ('This PDF has no selectable text, so it looks like a scanned image. Scanned CVs are not '
                      'supported yet: create your CV step by step instead.')),
    'busy': (503, 'CV import is busy. Please try again shortly.'),
}


class ImportFileError(Exception):
    """The file cannot be imported. `code` is a key of ERRORS."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def status(self) -> int:
        return ERRORS[self.code][0]

    @property
    def message(self) -> str:
        return ERRORS[self.code][1]


@dataclass(frozen=True)
class ExtractedText:
    text: str  # reading order
    pages: int
    method: str  # 'pdf-text' (selectable text). An OCR method can be added later for scanned PDFs.
    layout_text: str | None = None  # the same text kept in visual lines (multi-column CVs), when available


def _worker_env() -> dict[str, str]:
    # Only what Python needs to start: no FlowJob settings, database URI or API keys reach the parser process.
    return {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TMPDIR') if key in os.environ} | {
        'LANG': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8'}


async def _run_worker(data: bytes) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-I', str(_WORKER),
        str(MAX_PAGES), str(MAX_TEXT_CHARS), str(WORKER_CPU_SECONDS), str(WORKER_MEMORY_BYTES),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=_worker_env(), cwd=_WORKER.parent,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout=WORKER_TIMEOUT_SECONDS)
    except TimeoutError:
        raise ImportFileError('too_complex') from None
    finally:
        # Also covers a cancelled request: the parser never outlives it.
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0 or not stdout or len(stdout) > _MAX_WORKER_OUTPUT:
        # Killed by the CPU or memory limit, or crashed.
        raise ImportFileError('too_complex' if process.returncode and process.returncode < 0 else 'unreadable')
    try:
        result = json.loads(stdout)
    except ValueError:
        raise ImportFileError('unreadable') from None
    if not isinstance(result, dict):
        raise ImportFileError('unreadable')
    return result


def _clean(text: str) -> str:
    # Control characters (and NUL) are never valid CV text; keep line breaks and tabs, which carry layout.
    return _CONTROL.sub(' ', text).replace('\r\n', '\n').replace('\r', '\n').strip()


async def extract_text(data: bytes) -> ExtractedText:
    if not data:
        raise ImportFileError('empty')
    if len(data) > MAX_PDF_BYTES:
        raise ImportFileError('too_large')
    # The file must start with the PDF header (after an optional byte-order mark or whitespace), so a file of another
    # type that merely contains "%PDF-" is refused. The upload's declared type is never trusted on its own.
    if not data.removeprefix(b'\xef\xbb\xbf').lstrip()[:5] == b'%PDF-':
        raise ImportFileError('not_pdf')

    borrower = object()
    try:
        _slots.acquire_on_behalf_of_nowait(borrower)
    except WouldBlock:
        raise ImportFileError('busy') from None
    try:
        result = await _run_worker(data)
    finally:
        _slots.release_on_behalf_of(borrower)

    if not result.get('ok'):
        code = result.get('code')
        raise ImportFileError(code if code in ERRORS else 'unreadable')
    text, pages, layout = result.get('text'), result.get('pages'), result.get('layout')
    if not isinstance(text, str) or not isinstance(pages, int):
        raise ImportFileError('unreadable')
    text = _clean(text)
    if sum(ch.isalnum() for ch in text) < MIN_TEXT_CHARS:
        raise ImportFileError('no_text')
    layout = _clean(layout) if isinstance(layout, str) else None
    return ExtractedText(text=text, pages=pages, method='pdf-text', layout_text=layout or None)
