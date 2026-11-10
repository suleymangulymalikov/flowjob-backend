"""Nested CV contracts and legacy adapters, using synthetic data in disposable MongoDB."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from beanie import PydanticObjectId
from fastapi import HTTPException

from app.cv import render
from app.cv.content import MAX_CV_BODY_BYTES
from app.cv.http import render_pdf
from app.cv.revision import content_revision
from app.integration import router as cv_api
from app.integration.models import SavedCv
from app.services import ai_matching, ai_tailoring
from tests.test_saved_cv_pdf import BASE, _create_cv, _signup

PREVIEW = f'{BASE}/cvs/preview/pdf'
JOB = 'Backend developer with Python and MongoDB. You will design APIs and maintain a service with automated tests.'
PDF = b'%PDF-fixture'


@pytest.mark.parametrize('content', [
    'not an object',
    {'experience': {}},
    {'experience': ['not an object']},
    {'experience': [{'bullets': 'not a list'}]},
    {'experience': [{'bullets': ['valid text', 12]}]},
    {'education': [{'start': 2020}]},
    {'projects': [{'description': ['not text']}]},
    {'skills': [False]},
    {'languages': [{'name': 'English'}]},
    {'links': None},
    {'location': 'private-fixture\x00'},
    {'summary': '\ud800'},
    {'unknown': 'private-fixture'},
])
def test_malformed_cv_returns_sanitized_422_before_write_or_preview(client, monkeypatch, content):
    headers = _signup(client)
    cv = _create_cv(client, headers, {'summary': 'Saved text'})

    def no_render(*args):
        pytest.fail('Invalid content reached the PDF renderer.')

    monkeypatch.setattr(cv_api, 'render_saved_cv_pdf', no_render)
    request = {'headers': {'Content-Type': 'application/json', **headers},
               'content': json.dumps({'content': content})}
    for response in (
        client.post(f'{BASE}/cvs', **request),
        client.put(f'{BASE}/cvs/{cv}', **request),
        client.post(PREVIEW, **request),
    ):
        assert response.status_code == 422
        assert response.json()['detail'][0]['loc'][:2] == ['body', 'content']
        assert all('input' not in issue for issue in response.json()['detail'])
        assert 'private-fixture' not in response.text
    assert [row['id'] for row in client.get(f'{BASE}/cvs', headers=headers).json()] == [cv]
    assert client.get(f'{BASE}/cvs/{cv}', headers=headers).json()['content']['summary'] == 'Saved text'


def test_preview_optional_sections_and_free_form_values(client):
    headers = _signup(client)
    data = {
        'location': 'Wroclaw/Poland', 'phone': '+48 (555) 0100 ext. 4',
        'languages': 'English - C1 / Arabic - Native', 'links': ['github.com/jane', 'Portfolio'],
        'experience': [{'title': 'Developer', 'start': '0000-01', 'end': 'Summer 2019'}],
    }
    response = client.post(f'{BASE}/cvs', headers=headers, json={'content': data})
    assert response.status_code == 201
    content = response.json()['content']
    for key in ('phone', 'location', 'languages', 'links'):
        assert content[key] == data[key]
    assert content['projects'] == content['skills'] == content['education'] == []
    assert content['experience'][0]['start'] == '0000-01'
    assert render.format_dates_loose('0000-01', 'Summer 2019') == '0000-01 – Summer 2019'
    assert client.post(PREVIEW, headers=headers, json={'content': content}).content.startswith(b'%PDF-')
    assert client.post(PREVIEW, headers=headers, json={}).status_code == 200
    assert client.get(f'{BASE}/cvs', headers=headers).json()[0]['content'] == content


@pytest.mark.parametrize('link', [
    'javascript:alert(1)', 'JaVa\tScript:alert(1)', 'file:///private/fixture',
    'data:text/html,fixture', 'https://user:password@github.com/jane',
])
def test_new_unsafe_links_are_rejected_before_render_or_storage(client, monkeypatch, link):
    headers = _signup(client)

    def no_render(*args):
        pytest.fail('Unsafe contact link reached the PDF renderer.')

    monkeypatch.setattr(cv_api, 'render_saved_cv_pdf', no_render)
    payload = {'content': {'links': [link]}}
    assert client.post(f'{BASE}/cvs', headers=headers, json=payload).status_code == 422
    assert client.post(PREVIEW, headers=headers, json=payload).status_code == 422
    assert client.get(f'{BASE}/cvs', headers=headers).json() == []


def test_legacy_content_is_projected_for_read_and_work_without_rewriting_storage(client, monkeypatch):
    headers = _signup(client)
    cv_id = _create_cv(client, headers, {'summary': 'Seed'})
    raw = {
        'location': 'Berlin, Germany', 'phone': '555-0100', 'links': ['javascript:alert(1)', 'github.com/jane'],
        'summary': 'Engineer who builds APIs.',
        'experience': [None, {'title': 'Developer', 'start': 'Summer 2019', 'end': None,
                              'bullets': [None, 'Built REST APIs with Python and MongoDB']}],
        'education': None, 'projects': {'old': 'unrecognized'},
        'skills': ['Python', 42], 'languages': [{'name': 'English', 'level': 'C1'}],
        'private-fixture': 'unrecognized legacy metadata',
    }
    collection = SavedCv.get_pymongo_collection()
    oid = PydanticObjectId(cv_id)

    async def seed():
        await collection.update_one({'_id': oid}, {'$set': {'content': raw}})

    async def stored():
        return (await collection.find_one({'_id': oid}))['content']

    client.portal.call(seed)
    saved = client.get(f'{BASE}/cvs/{cv_id}', headers=headers).json()
    adapted = saved['content']
    assert saved['contentRevision'] == content_revision(raw)
    assert adapted['links'] == raw['links']
    assert adapted['phone'] == raw['phone'] and adapted['location'] == raw['location']
    assert adapted['education'] == adapted['projects'] == []
    assert adapted['languages'] == 'English (C1)' and adapted['skills'] == ['Python']
    assert adapted['experience'][0]['id'] == 'legacy-experience-0'
    assert adapted['experience'][0]['bullets'] == ['', 'Built REST APIs with Python and MongoDB']

    assert client.get(f'{BASE}/cvs/{cv_id}/pdf', headers=headers).content.startswith(b'%PDF-')
    assert client.post(PREVIEW, headers=headers, json={'cvId': cv_id, 'content': adapted}).status_code == 200
    assert client.post(PREVIEW, headers=headers, json={'content': adapted}).status_code == 422
    other = _signup(client, 'other@example.com')
    assert client.post(PREVIEW, headers=other, json={'cvId': cv_id, 'content': adapted}).status_code == 404

    for workflow in ('matches', 'tailor'):
        response = client.post(f'{BASE}/{workflow}', headers=headers, json={
            'cvId': cv_id, 'jobTitle': 'Dev', 'jobDescription': JOB,
        })
        assert response.status_code == 201
        if workflow == 'tailor':
            draft = response.json()
            assert draft['sourceCvRevision'] == content_revision(raw)
            assert client.post(PREVIEW, headers=headers, json={
                'tailorDraftId': draft['id'], 'content': adapted,
            }).status_code == 200
            tailored = client.post(f'{BASE}/cvs', headers=headers, json={
                'kind': 'tailored', 'tailorDraftId': draft['id'], 'sourceCvId': cv_id, 'content': adapted,
            })
            assert tailored.status_code == 201 and tailored.json()['sourceCvId'] == cv_id

    assert client.put(f'{BASE}/cvs/{cv_id}', headers=headers, json={'name': 'Renamed'}).status_code == 200
    assert client.put(f'{BASE}/cvs/{cv_id}', headers=headers, json={'content': adapted}).status_code == 200
    assert client.portal.call(stored) == raw
    assert client.get(f'{BASE}/cvs/{cv_id}', headers=headers).json()['contentRevision'] == content_revision(raw)
    extra_link = {**adapted, 'links': [*adapted['links'], adapted['links'][0]]}
    assert client.put(f'{BASE}/cvs/{cv_id}', headers=headers, json={'content': extra_link}).status_code == 422
    assert client.portal.call(stored) == raw


@pytest.mark.parametrize('content', [
    {'summary': 'x' * 8001},
    {'experience': [{}] * 101},
    {'experience': [{'bullets': ['x'] * 41}]},
    {'experience': [{'bullets': ['x'] * 40}] * 15},
    {'projects': [{'description': 'é' * 4000}] * 9},
])
def test_cv_limits_reject_oversized_content(client, content):
    headers = _signup(client)
    payload = {'content': content}
    assert client.post(f'{BASE}/cvs', headers=headers, json=payload).status_code == 422
    assert client.post(PREVIEW, headers=headers, json=payload).status_code == 422


def test_wire_limit_handles_content_length_and_chunked_requests(client):
    headers = {'Content-Type': 'application/json', **_signup(client)}
    data = b' ' * MAX_CV_BODY_BYTES + json.dumps({'content': {}}).encode()
    for body in (data, iter([data[:120000], data[120000:]])):
        response = client.post(PREVIEW, headers=headers, content=body)
        assert response.status_code == 413
        assert '256 KiB' in response.json()['detail']


def test_over_budget_legacy_cv_is_readable_but_rejected_before_ai_or_pdf_work(client, monkeypatch):
    headers = _signup(client)
    cv_id = _create_cv(client, headers, {'summary': 'Seed'})
    oversized = {'experience': [{'bullets': ['x'] * 40}] * 15}

    async def seed():
        await SavedCv.get_pymongo_collection().update_one(
            {'_id': PydanticObjectId(cv_id)}, {'$set': {'content': oversized}},
        )

    client.portal.call(seed)

    def no_work(*args):
        pytest.fail('An oversized legacy CV reached expensive work.')

    monkeypatch.setattr(cv_api, 'render_saved_cv_pdf', no_work)
    monkeypatch.setattr(ai_matching, 'analyze', no_work)
    monkeypatch.setattr(ai_tailoring, 'analyze', no_work)
    assert len(client.get(f'{BASE}/cvs/{cv_id}', headers=headers).json()['content']['experience']) == 15
    assert client.get(f'{BASE}/cvs/{cv_id}/pdf', headers=headers).status_code == 422
    for workflow in ('matches', 'tailor'):
        response = client.post(f'{BASE}/{workflow}', headers=headers, json={
            'cvId': cv_id, 'jobTitle': 'Dev', 'jobDescription': JOB,
        })
        assert response.status_code == 422


def test_render_failure_is_sanitized_and_releases_slot(client, monkeypatch):
    headers = _signup(client)

    def fail(*args):
        raise RuntimeError('private renderer internals')

    with monkeypatch.context() as patch:
        patch.setattr(cv_api, 'render_saved_cv_pdf', fail)
        response = client.post(PREVIEW, headers=headers, json={'content': {}})
    assert response.status_code == 503
    assert response.json() == {'detail': 'Could not generate the PDF. Please try again.'}
    assert client.post(PREVIEW, headers=headers, json={'content': {}}).status_code == 200


def test_render_enforces_page_budget_and_disables_external_resource_fetching(client, monkeypatch):
    headers = _signup(client)
    render_attempts = 0

    class TooManyPages:
        pages = [object()] * 17

        def write_pdf(self):
            pytest.fail('A document above the page budget was serialized.')

    class FakeHTML:
        def __init__(self, *, string, url_fetcher):
            for url in ('https://example.com/test', 'file:///private/fixture', 'data:text/plain,fixture'):
                with pytest.raises(ValueError, match='disallowed protocol'):
                    url_fetcher.fetch(url)

        def render(self):
            nonlocal render_attempts
            render_attempts += 1
            return TooManyPages()

    monkeypatch.setattr(render, 'HTML', FakeHTML)
    response = client.post(PREVIEW, headers=headers, json={'content': {}})
    assert response.status_code == 422 and '16-page PDF limit' in response.json()['detail']
    assert render_attempts == 2


def test_concurrent_preview_and_download_share_two_slots_and_excess_has_no_queue(client, monkeypatch):
    headers = _signup(client)
    cv_id = _create_cv(client, headers, {'summary': 'Seed'})
    lock, both_started, finish = Lock(), Event(), Event()
    active = 0
    peak = 0

    def blocking_renderer(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
        try:
            assert finish.wait(10)
            return PDF
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(cv_api, 'render_saved_cv_pdf', blocking_renderer)

    def preview():
        return client.post(PREVIEW, headers=headers, json={'content': {}})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(preview)
        second = pool.submit(client.get, f'{BASE}/cvs/{cv_id}/pdf', headers=headers)
        try:
            assert both_started.wait(5)
            for response in (preview(), client.get('/cv/pdf', headers=headers)):
                assert response.status_code == 503 and response.headers['Retry-After'] == '1'
        finally:
            finish.set()
        assert first.result(timeout=5).content == second.result(timeout=5).content == PDF
    assert peak == 2 and preview().content == PDF


def test_cancelled_request_retains_slot_until_renderer_finishes(client):
    async def exercise():
        loop = asyncio.get_running_loop()
        started = [asyncio.Event(), asyncio.Event()]
        finished = [asyncio.Event(), asyncio.Event()]
        release = Event()

        def blocked(index):
            loop.call_soon_threadsafe(started[index].set)
            try:
                assert release.wait(5)
                return PDF
            finally:
                loop.call_soon_threadsafe(finished[index].set)

        first = asyncio.create_task(render_pdf(blocked, 0))
        second = asyncio.create_task(render_pdf(blocked, 1))
        try:
            await asyncio.wait_for(asyncio.gather(*(signal.wait() for signal in started)), 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(HTTPException) as error:
                await render_pdf(lambda: PDF)
            assert error.value.status_code == 503
        finally:
            release.set()
        assert await second == PDF
        await asyncio.wait_for(asyncio.gather(*(signal.wait() for signal in finished)), 5)

    client.portal.call(exercise)
