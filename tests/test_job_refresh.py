"""Polite, self-healing refresh: retries on rate limits and earlier retry of failed companies."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.jobs import fetchers, service
from app.jobs.fetchers import FetchError
from app.jobs.models import JobSource


def run_get(responses):
    """Call fetchers._get against a fake server that answers with `responses` in order."""
    answers = iter(responses)
    waits = []

    def handler(request):
        return next(answers)

    async def fake_sleep(seconds):
        waits.append(seconds)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetchers._get(client, 'https://example.com/jobs', sleep=fake_sleep)
    return asyncio.run(go()), waits


def test_get_retries_rate_limits_and_honours_retry_after():
    data, waits = run_get([
        httpx.Response(429),
        httpx.Response(503, headers={'Retry-After': '7'}),
        httpx.Response(200, json={'jobs': []}),
    ])
    assert data == {'jobs': []}
    assert waits == [2.0, 7.0]  # default first wait, then the job board's own hint


def test_get_caps_long_retry_after_and_gives_up_after_three_retries():
    with pytest.raises(FetchError, match='HTTP 429'):
        run_get([httpx.Response(429, headers={'Retry-After': '600'})] * 4)


def test_get_does_not_retry_other_errors():
    with pytest.raises(FetchError, match='HTTP 404'):
        run_get([httpx.Response(404), httpx.Response(200, json={})])


def test_failed_company_is_retried_after_30_minutes(client, monkeypatch):
    companies = [{'company': 'Acme', 'vendor': 'greenhouse', 'slug': 'acme'},
                 {'company': 'Beta', 'vendor': 'workable', 'slug': 'beta'}]
    monkeypatch.setattr(service, 'load_companies', lambda: companies)
    now = datetime.now(UTC)

    async def setup(beta_ok, beta_age):
        await JobSource.delete_all()
        await JobSource(key='greenhouse:acme', company='Acme', vendor='greenhouse', slug='acme',
                        last_fetched_at=now - timedelta(minutes=5), last_ok=True).insert()
        await JobSource(key='workable:beta', company='Beta', vendor='workable', slug='beta',
                        last_fetched_at=now - beta_age, last_ok=beta_ok, last_error=None if beta_ok else 'HTTP 429').insert()
        return await service.is_stale()

    assert client.portal.call(setup, True, timedelta(minutes=45)) is False   # healthy: normal 6-hour interval
    assert client.portal.call(setup, False, timedelta(minutes=10)) is False  # failed, but only 10 minutes ago
    assert client.portal.call(setup, False, timedelta(minutes=45)) is True   # failed 45 minutes ago: retry now


def test_rate_limited_job_board_is_skipped_for_the_rest_of_the_refresh(client):
    calls = []

    async def workable_blocked(http, source):
        calls.append(source['slug'])
        raise FetchError(f"HTTP 429 for https://apply.workable.com/{source['slug']}")

    async def greenhouse_ok(http, source):
        calls.append(source['slug'])
        return []

    companies = [{'company': f'W{i}', 'vendor': 'workable', 'slug': f'w{i}'} for i in range(4)] + \
                [{'company': 'G', 'vendor': 'greenhouse', 'slug': 'g'}]
    client.portal.call(lambda: service.refresh_all(companies, {'workable': workable_blocked, 'greenhouse': greenhouse_ok}))
    assert [c for c in calls if c.startswith('w')] == ['w0']  # only the first Workable company was asked
    assert 'g' in calls  # other job boards are not affected
    sources = {s.key: s for s in client.portal.call(JobSource.find_all().to_list)}
    assert sources['workable:w3'].last_ok is False and 'Skipped' in sources['workable:w3'].last_error
    assert sources['greenhouse:g'].last_ok is True


def test_workable_companies_are_fetched_one_at_a_time(client, monkeypatch):
    running = {'workable': 0, 'max_workable': 0}

    async def slow_fetch(http, source):
        if source['vendor'] == 'workable':
            running['workable'] += 1
            running['max_workable'] = max(running['max_workable'], running['workable'])
        await asyncio.sleep(0.02)
        if source['vendor'] == 'workable':
            running['workable'] -= 1
        return []

    companies = [{'company': f'W{i}', 'vendor': 'workable', 'slug': f'w{i}'} for i in range(5)] + \
                [{'company': f'G{i}', 'vendor': 'greenhouse', 'slug': f'g{i}'} for i in range(5)]
    client.portal.call(lambda: service.refresh_all(companies, {'workable': slow_fetch, 'greenhouse': slow_fetch}))
    assert running['max_workable'] == 1
