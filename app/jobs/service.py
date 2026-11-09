"""Keep the job cache fresh.

- The list of companies lives in companies.json (vendor + slug + display name).
- A refresh fetches every company (4 at a time) and updates MongoDB.
- Refreshes run in the background, at most one at a time, only when the cache is older than
  JOBS_REFRESH_HOURS. Users always read from MongoDB, so the page stays fast.
- If a job board is down, that company's previous jobs are kept and the error is recorded.
"""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from beanie.operators import In, NotIn

from app.config import settings
from app.jobs.fetchers import FETCHERS, FetchError, fetch_description
from app.jobs.models import JobPosting, JobSource
from app.jobs.skills import SKILLS_VERSION, job_skills

log = logging.getLogger(__name__)
COMPANIES_FILE = Path(__file__).parent / 'companies.json'
CONCURRENCY = 4
DETAIL_CONCURRENCY = 4

_lock = asyncio.Lock()
_task: asyncio.Task | None = None


def load_companies() -> list[dict]:
    return json.loads(COMPANIES_FILE.read_text(encoding='utf-8'))


def source_key(company: dict) -> str:
    return f"{company['vendor']}:{company['slug']}"


def as_utc(d: datetime | None) -> datetime | None:
    """MongoDB returns naive datetimes; treat them as UTC so comparisons work."""
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _reusable_skills(old: JobPosting | None, job: dict) -> dict | None:
    """Skills stored last time, if the job's title and description did not change since."""
    if (old and old.skills and old.skills.get('version') == SKILLS_VERSION
            and old.title == job['title'] and old.description == job['description']):
        return old.skills
    return None


async def _with_skills(jobs: list[dict], previous: list[JobPosting | None]) -> list[dict]:
    """Add keyword skills to each job. Only new or changed jobs are analysed (~20 ms each),
    in a worker thread so the web server stays responsive while users are browsing."""
    def compute():
        return [{**j, 'skills': _reusable_skills(old, j) or job_skills(j['title'], j['description'])}
                for j, old in zip(jobs, previous)]
    return await asyncio.to_thread(compute)


async def refresh_company(client: httpx.AsyncClient, company: dict, fetchers=FETCHERS,
                          describe=fetch_description, detail_semaphore: asyncio.Semaphore | None = None) -> int:
    """Fetch one company and sync its postings. Returns the number of Polish jobs found."""
    key = source_key(company)
    source = await JobSource.find_one(JobSource.key == key) or JobSource(
        key=key, company=company['company'], vendor=company['vendor'], slug=company['slug'])
    source.company = company['company']
    now = datetime.now(UTC)
    try:
        jobs = await fetchers[company['vendor']](client, company)
    except FetchError as e:
        source.last_fetched_at, source.last_ok, source.last_error = now, False, str(e)[:300]
        await source.save()
        log.warning('Job source %s failed: %s', key, e)
        return 0

    fresh = {f"{key}:{j['external_id']}": j for j in jobs}  # also removes duplicates
    existing = {p.key: p for p in await JobPosting.find(In(JobPosting.key, list(fresh))).to_list()}

    # Keep descriptions loaded earlier; load missing ones (SmartRecruiters) once, so every job can be scored.
    detail_semaphore = detail_semaphore or asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def complete(posting_key: str, job: dict) -> dict:
        if job['description'] is None and posting_key in existing:
            job = {**job, 'description': existing[posting_key].description}
        if job['description'] is None and settings.jobs_live_fetch:
            async with detail_semaphore:
                try:
                    job = {**job, 'description': await describe(client, job['vendor'], job['slug'], job['external_id'])}
                except FetchError as e:
                    log.warning('Description for %s failed: %s', posting_key, e)
        return job

    completed = await asyncio.gather(*(complete(k, j) for k, j in fresh.items()))
    analysed = await _with_skills(list(completed), [existing.get(k) for k in fresh])
    for posting_key, job in zip(fresh, analysed):
        posting = existing.get(posting_key)
        if posting:
            for field, value in job.items():
                setattr(posting, field, value)
            posting.last_seen_at = now
            await posting.save()
        else:
            await JobPosting(key=posting_key, first_seen_at=now, last_seen_at=now, **job).insert()
    # Jobs the company no longer lists are closed: remove them.
    await JobPosting.find(JobPosting.vendor == company['vendor'], JobPosting.slug == company['slug'],
                          NotIn(JobPosting.key, list(fresh))).delete()

    source.last_fetched_at, source.last_ok, source.last_error, source.job_count = now, True, None, len(fresh)
    await source.save()
    return len(fresh)


async def refresh_all(companies: list[dict] | None = None, fetchers=FETCHERS) -> dict:
    """Refresh every company. Returns a small summary for logs and tests."""
    companies = companies if companies is not None else load_companies()
    async with _lock:
        # Companies removed from companies.json: drop their cached jobs too.
        wanted = [source_key(c) for c in companies]
        removed = await JobSource.find(NotIn(JobSource.key, wanted)).to_list()
        for s in removed:
            await JobPosting.find(JobPosting.vendor == s.vendor, JobPosting.slug == s.slug).delete()
            await s.delete()

        semaphore = asyncio.Semaphore(CONCURRENCY)
        detail_semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)  # shared: at most 4 description requests at once
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            async def one(company):
                async with semaphore:
                    return await refresh_company(client, company, fetchers, detail_semaphore=detail_semaphore)
            counts = await asyncio.gather(*(one(c) for c in companies))
    summary = {'companies': len(companies), 'jobs': sum(counts)}
    log.info('Job refresh finished: %s', summary)
    return summary


async def is_stale() -> bool:
    companies = load_companies()
    sources = await JobSource.find(In(JobSource.key, [source_key(c) for c in companies])).to_list()
    if len(sources) < len(companies):
        return True
    limit = datetime.now(UTC) - timedelta(hours=settings.jobs_refresh_hours)
    if any(as_utc(s.last_fetched_at) is None or as_utc(s.last_fetched_at) < limit for s in sources):
        return True
    # Jobs stored before the skill rules changed (or before skills existed) need recalculating.
    outdated = await JobPosting.find({'description': {'$ne': None}, 'skills.version': {'$ne': SKILLS_VERSION}}).count()
    return outdated > 0


def is_refreshing() -> bool:
    return _task is not None and not _task.done()


async def ensure_fresh() -> bool:
    """Start a background refresh when the cache is old. Returns True while one is running."""
    global _task
    if not settings.jobs_live_fetch:
        return False
    if is_refreshing():
        return True
    if await is_stale():
        _task = asyncio.create_task(_refresh_logged())
        return True
    return False


async def _refresh_logged() -> None:
    try:
        await refresh_all()
    except Exception:  # a background task must never crash silently
        log.exception('Job refresh crashed')


async def last_refreshed_at() -> datetime | None:
    source = await JobSource.find(JobSource.last_ok == True).sort(-JobSource.last_fetched_at).first_or_none()  # noqa: E712
    return as_utc(source.last_fetched_at) if source else None


async def load_description(posting: JobPosting) -> str | None:
    """Some vendors (SmartRecruiters) only give the description in a second request: load and cache it."""
    if posting.description is not None or not settings.jobs_live_fetch:
        return posting.description
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            description = await fetch_description(client, posting.vendor, posting.slug, posting.external_id)
    except FetchError as e:
        log.warning('Description for %s failed: %s', posting.key, e)
        return None
    if description:
        posting.description = description
        posting.skills = await asyncio.to_thread(job_skills, posting.title, description)
        await posting.save()
    return description
