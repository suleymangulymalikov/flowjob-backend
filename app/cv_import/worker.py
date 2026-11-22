"""Isolated PDF text extraction. Runs as its own process: `python -I worker.py <max_pages> <max_chars> <cpu> <memory>`.

The parent (app/cv_import/extract.py) writes the PDF to stdin and reads one JSON object from stdout:
{"ok": true, "pages": n, "text": "...", "layout": "..." | null} or {"ok": false, "code": "<reason>"}.
`text` is pypdf's reading order; `layout` keeps each visual line together (so a right-aligned date stays on its job's
line). The layout version is optional: if it cannot be produced within its budget, it is null.

Untrusted PDFs are parsed here, never in the API process: the process gets a CPU-time limit, a memory limit (where
the OS supports one), no permission to write files, a minimal environment without credentials, and the parent kills
it after a wall-clock timeout. Only the standard library and pypdf are imported, so it never loads FlowJob settings.
Errors are reported as fixed codes; parser messages are never passed on, so no PDF content reaches the logs.
"""

import io
import json
import re
import resource
import sys

_SPACES = re.compile(r' {3,}')


def _limit(cpu_seconds: int, memory_bytes: int) -> None:
    limits = (
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, 0),  # no files may be written
        (resource.RLIMIT_AS, memory_bytes),  # enforced on Linux; macOS may refuse it (the CPU and wall-clock limits remain)
    )
    for name, value in limits:
        try:
            resource.setrlimit(name, (value, value))
        except (ValueError, OSError):
            pass


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def _fail(code: str) -> None:
    _emit({'ok': False, 'code': code})


def main() -> None:
    max_pages, max_chars, cpu_seconds, memory_bytes = (int(value) for value in sys.argv[1:5])
    _limit(cpu_seconds, memory_bytes)
    data = sys.stdin.buffer.read()
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            # PDFs restricted only by an owner password open with an empty user password; anything else is locked.
            try:
                opened = reader.decrypt('')
            except Exception:  # noqa: BLE001 - any decryption failure means the PDF stays locked
                opened = 0
            if not opened:
                return _fail('encrypted')
        pages = len(reader.pages)
        if pages == 0:
            return _fail('unreadable')
        if pages > max_pages:
            return _fail('too_many_pages')
        parts, total = [], 0
        for page in reader.pages:
            text = page.extract_text() or ''
            total += len(text)
            if total > max_chars:
                return _fail('too_much_text')
            parts.append(text)
        layout = _layout(reader, max_chars * 2)
    except MemoryError:
        return _fail('too_complex')
    except Exception:  # noqa: BLE001 - a malformed PDF can raise anything; only a fixed code leaves this process
        return _fail('unreadable')
    _emit({'ok': True, 'pages': pages, 'text': '\n\n'.join(parts), 'layout': layout})


def _layout(reader, budget: int) -> str | None:
    """Visual-line text, with runs of padding spaces shortened. None when it fails or exceeds `budget`."""
    pages, total = [], 0
    try:
        for page in reader.pages:
            text = '\n'.join(_SPACES.sub('   ', line).rstrip()
                             for line in (page.extract_text(extraction_mode='layout') or '').splitlines())
            total += len(text)
            if total > budget:
                return None
            pages.append(text)
    except Exception:  # noqa: BLE001 - the reading-order text is enough on its own
        return None
    return '\n\n'.join(pages)


if __name__ == '__main__':
    main()
