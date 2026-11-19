"""Job identity and request coalescing for the AI endpoints."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field

from app.config import settings


@dataclass
class _Flight:
    fresh: bool
    task: asyncio.Task = field(init=False)


_flights: dict[tuple[Hashable, ...], _Flight] = {}


def job_fingerprint(title: str, company: str | None, url: str | None, context: str) -> str:
    # Keep exact metadata and delimit values unambiguously, without storing the job text.
    encoded = json.dumps(['job-context-v2', title, company, url, context], ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def cache_model() -> str:
    return 'mock' if settings.mock_ai else settings.openai_model


async def coalesce_request[T](
    identity: tuple[Hashable, ...],
    lookup: Callable[[], Awaitable[T | None]],
    generate: Callable[[], Awaitable[T]],
    *,
    fresh: bool = False,
) -> T:
    """Share one in-flight result or error for this identity within a process."""
    flight = _flights.get(identity)
    if flight is None or flight.task.done():
        flight = _flights[identity] = _Flight(fresh)

        async def run() -> T:
            if not flight.fresh:
                cached = await lookup()
                # An explicit fresh request may have joined during the lookup.
                if cached is not None and not flight.fresh:
                    return cached
            return await generate()

        def discard(task: asyncio.Task) -> None:
            if _flights.get(identity) is flight:
                del _flights[identity]
            if not task.cancelled():
                # Observe failures even if every caller has disconnected.
                task.exception()

        flight.task = asyncio.create_task(run())
        flight.task.add_done_callback(discard)
    elif fresh:
        flight.fresh = True

    # A disconnected waiter cannot cancel generation or its quota settlement.
    return await asyncio.shield(flight.task)
