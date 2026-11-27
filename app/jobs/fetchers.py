"""Read open jobs from public, documented job-board APIs and keep only jobs in Poland.

Every fetcher returns a list of plain dicts with the same keys (see `_job`). No API keys are
needed: these endpoints are published by the job-board vendors so companies can show their
jobs on other sites. Each company is one HTTP request (SmartRecruiters: one per 100 jobs).
"""
import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.jobs.classify import INTERNSHIP, ENTRY, classify_level, html_to_text, polish_city

USER_AGENT = 'FlowJob/0.1 (student project; job listings for students)'
MAX_DESCRIPTION = 20_000


class FetchError(Exception):
    """The job board could not be read (network error, bad status, unexpected format)."""


def _date(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: milliseconds since epoch
            return datetime.fromtimestamp(value / 1000, UTC)
        text = str(value).strip().replace('Z', '+00:00').replace(' UTC', '+00:00')  # Recruitee: '... UTC'
        d = datetime.fromisoformat(text)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


def _job(source: dict, external_id: Any, title: str | None, location: str, url: str | None,
         posted_at: Any = None, description: str | None = None, structured_level: str | None = None) -> dict | None:
    """Build the common job dict, or None when the job is not in Poland."""
    city = polish_city(location)
    if city is None or not title:
        return None
    level, signal = classify_level(title, structured_level, description or '')
    return {
        'external_id': str(external_id),
        'title': title.strip(),
        'company': source['company'],
        'vendor': source['vendor'],
        'slug': source['slug'],
        'location': location.strip(' |'),
        'city': city,
        'url': _safe_url(url),
        'level': level,
        'level_signal': signal,
        'description': description[:MAX_DESCRIPTION] if description else None,
        'posted_at': _date(posted_at),
    }


def _safe_url(url: Any) -> str | None:
    """Only plain web links: a job board's data is shown to users as a clickable link."""
    text = str(url).strip() if url else ''
    return text if text.lower().startswith(('https://', 'http://')) and len(text) <= 2048 else None


RETRY_STATUS = {429, 503}  # "too many requests" / "busy": wait and try again
RETRY_WAITS = [2.0, 5.0, 10.0]  # seconds; a Retry-After header from the job board wins (capped at MAX_WAIT)
MAX_WAIT = 30.0


def _retry_after(r: httpx.Response, default: float) -> float:
    try:
        return min(float(r.headers.get('Retry-After', default)), MAX_WAIT)
    except ValueError:
        return default


async def _get(client: httpx.AsyncClient, url: str, sleep=asyncio.sleep) -> Any:
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            r = await client.get(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
        except httpx.HTTPError as e:
            raise FetchError(f'{type(e).__name__} for {url}') from e
        if r.status_code in RETRY_STATUS and attempt < len(RETRY_WAITS):
            await sleep(_retry_after(r, RETRY_WAITS[attempt]))
            continue
        if r.status_code != 200:
            raise FetchError(f'HTTP {r.status_code} for {url}')
        try:
            return r.json()
        except ValueError as e:
            raise FetchError(f'Not JSON: {url}') from e


async def fetch_greenhouse(client: httpx.AsyncClient, source: dict) -> list[dict]:
    data = await _get(client, f"https://boards-api.greenhouse.io/v1/boards/{source['slug']}/jobs?content=true")
    out = []
    for j in data.get('jobs', []):
        location = ' | '.join([(j.get('location') or {}).get('name') or ''] +
                              [f"{o.get('name') or ''} {o.get('location') or ''}" for o in j.get('offices') or []])
        out.append(_job(source, j.get('id'), j.get('title'), location, j.get('absolute_url'),
                        j.get('first_published') or j.get('updated_at'), html_to_text(j.get('content'))))
    return [x for x in out if x]


async def fetch_lever(client: httpx.AsyncClient, source: dict) -> list[dict]:
    slug = source['slug']
    host = 'api.eu.lever.co' if slug.startswith('eu:') else 'api.lever.co'
    data = await _get(client, f"https://{host}/v0/postings/{slug.split(':', 1)[-1]}?mode=json")
    if not isinstance(data, list):
        raise FetchError('Unexpected Lever response')
    out = []
    for j in data:
        c = j.get('categories') or {}
        location = ' | '.join([c.get('location') or ''] + (c.get('allLocations') or []) +
                              (['Poland'] if j.get('country') == 'PL' else []))
        structured = INTERNSHIP if 'intern' in (c.get('commitment') or '').lower() else None
        parts = [j.get('descriptionPlain') or '']
        for section in j.get('lists') or []:
            parts.append(f"{section.get('text') or ''}\n{html_to_text(section.get('content'))}")
        parts.append(j.get('additionalPlain') or '')
        out.append(_job(source, j.get('id'), j.get('text'), location, j.get('hostedUrl'), j.get('createdAt'),
                        '\n\n'.join(p.strip() for p in parts if p.strip()), structured))
    return [x for x in out if x]


async def fetch_ashby(client: httpx.AsyncClient, source: dict) -> list[dict]:
    data = await _get(client, f"https://api.ashbyhq.com/posting-api/job-board/{source['slug']}")
    out = []
    for j in data.get('jobs', []):
        address = (j.get('address') or {}).get('postalAddress') or {}
        location = ' | '.join([j.get('location') or ''] +
                              [x.get('location') or '' for x in j.get('secondaryLocations') or []] +
                              [address.get('addressLocality') or '', address.get('addressCountry') or ''])
        structured = INTERNSHIP if (j.get('employmentType') or '').lower() == 'intern' else None
        out.append(_job(source, j.get('id'), j.get('title'), location, j.get('jobUrl'), j.get('publishedAt'),
                        j.get('descriptionPlain') or html_to_text(j.get('descriptionHtml')), structured))
    return [x for x in out if x]


async def fetch_smartrecruiters(client: httpx.AsyncClient, source: dict) -> list[dict]:
    """The list endpoint has no descriptions; `fetch_description` loads one when a user opens a job."""
    out, offset = [], 0
    while True:
        data = await _get(client, f"https://api.smartrecruiters.com/v1/companies/{source['slug']}/postings"
                                  f"?country=pl&limit=100&offset={offset}")
        for j in data.get('content', []):
            level_id = (j.get('experienceLevel') or {}).get('id')
            employment = (j.get('typeOfEmployment') or {}).get('id')
            structured = (INTERNSHIP if level_id == 'internship' or employment == 'intern'
                          else ENTRY if level_id == 'entry_level' else None)
            loc = j.get('location') or {}
            location = ' | '.join(x for x in [loc.get('city'), loc.get('region'), 'Poland'] if x)
            out.append(_job(source, j.get('id'), j.get('name'), location,
                            f"https://jobs.smartrecruiters.com/{source['slug']}/{j.get('id')}",
                            j.get('releasedDate'), None, structured))
        offset += 100
        if offset >= data.get('totalFound', 0) or offset >= 1000:
            break
    return [x for x in out if x]


async def fetch_recruitee(client: httpx.AsyncClient, source: dict) -> list[dict]:
    data = await _get(client, f"https://{source['slug']}.recruitee.com/api/offers/")
    out = []
    for o in data.get('offers', []):
        location = ' | '.join([o.get('location') or '', o.get('city') or '', o.get('country') or ''] +
                              [f"{x.get('city') or ''} {x.get('country') or ''}" for x in o.get('locations') or []] +
                              (['Poland'] if (o.get('country_code') or '').upper() == 'PL' else []))
        codes = f"{o.get('experience_code') or ''} {o.get('employment_type_code') or ''}".lower()
        structured = INTERNSHIP if 'intern' in codes else ENTRY if ('entry' in codes or 'student' in codes) else None
        description = '\n\n'.join(filter(None, [html_to_text(o.get('description')), html_to_text(o.get('requirements'))]))
        out.append(_job(source, o.get('id'), o.get('title'), location, o.get('careers_url'),
                        o.get('published_at') or o.get('created_at'), description, structured))
    return [x for x in out if x]


async def fetch_workable(client: httpx.AsyncClient, source: dict) -> list[dict]:
    """Workable's widget feed has no descriptions; users open the original posting instead."""
    data = await _get(client, f"https://apply.workable.com/api/v1/widget/accounts/{source['slug']}")
    out = []
    for j in data.get('jobs', []):
        location = ' | '.join([j.get('city') or '', j.get('country') or ''] +
                              [f"{x.get('city') or ''} {x.get('country') or ''}" for x in j.get('locations') or []])
        structured = INTERNSHIP if 'intern' in f"{j.get('experience') or ''} {j.get('employment_type') or ''}".lower() else None
        out.append(_job(source, j.get('shortcode') or j.get('id'), j.get('title'), location,
                        j.get('url') or j.get('shortlink'), j.get('published_on') or j.get('created_at'), None, structured))
    return [x for x in out if x]


FETCHERS = {
    'greenhouse': fetch_greenhouse,
    'lever': fetch_lever,
    'ashby': fetch_ashby,
    'smartrecruiters': fetch_smartrecruiters,
    'recruitee': fetch_recruitee,
    'workable': fetch_workable,
}


async def fetch_description(client: httpx.AsyncClient, vendor: str, slug: str, external_id: str) -> str | None:
    """Load the full description for vendors whose list endpoint does not include it."""
    if vendor != 'smartrecruiters':
        return None
    data = await _get(client, f'https://api.smartrecruiters.com/v1/companies/{slug}/postings/{external_id}')
    sections = (data.get('jobAd') or {}).get('sections') or {}
    parts = []
    for key in ('jobDescription', 'qualifications', 'additionalInformation', 'companyDescription'):
        s = sections.get(key) or {}
        text = html_to_text(s.get('text'))
        if text:
            parts.append(f"{s.get('title') or ''}\n{text}".strip())
    return '\n\n'.join(parts)[:MAX_DESCRIPTION] or None
