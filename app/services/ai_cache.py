"""Job identity and request coalescing for the AI endpoints."""

import asyncio
import hashlib
import json
from collections.abc import Hashable
from weakref import WeakValueDictionary

from app.config import settings

_locks: WeakValueDictionary[tuple[Hashable, ...], asyncio.Lock] = WeakValueDictionary()


def job_fingerprint(title: str, company: str | None, url: str | None, context: str) -> str:
    # Keep exact metadata and delimit values unambiguously, without storing the job text.
    encoded = json.dumps(['job-context-v2', title, company, url, context], ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def cache_model() -> str:
    return 'mock' if settings.openai_api_key == 'mock' else settings.openai_model


def cache_lock(*identity: Hashable) -> asyncio.Lock:
    """Serialize the lookup and generation for one identity in this server process."""
    lock = _locks.get(identity)
    if lock is None:
        lock = _locks[identity] = asyncio.Lock()
    return lock
