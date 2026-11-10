"""Job application tracking API (authenticated, owner-scoped, MongoDB-backed)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from beanie import PydanticObjectId

from app.applications import router as application_api
from app.applications.models import Application

BASE = '/api/v1'


def signup(client, email='ana@example.com'):
    r = client.post(f'{BASE}/auth/register', json={'name': 'Ana', 'email': email, 'password': 'secret123'})
    assert r.status_code == 201, r.text
    return {'Authorization': f"Bearer {r.json()['accessToken']}"}


def make_cv(client, headers, name='My CV'):
    r = client.post(f'{BASE}/cvs', json={'name': name, 'kind': 'original'}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()['id']


def test_requires_authentication(client):
    assert client.get(f'{BASE}/applications').status_code == 401
    assert client.post(f'{BASE}/applications', json={'company': 'X', 'jobTitle': 'Y'}).status_code == 401


def test_empty_list(client):
    headers = signup(client)
    assert client.get(f'{BASE}/applications', headers=headers).json() == []


def test_create_and_persist(client):
    headers = signup(client)
    r = client.post(f'{BASE}/applications', headers=headers, json={
        'company': 'Acme', 'jobTitle': 'Engineer', 'status': 'Applied',
        'jobUrl': 'acme.com/careers', 'location': 'Boston', 'workArrangement': 'Remote',
        'employmentType': 'Full-time', 'dateApplied': '2026-01-15', 'notes': 'Referred by a friend',
        'followUpDate': '2026-02-01',
    })
    assert r.status_code == 201, r.text
    a = r.json()
    assert a['company'] == 'Acme' and a['status'] == 'Applied' and a['workArrangement'] == 'Remote'
    assert a['dateApplied'] == '2026-01-15' and a['followUpDate'] == '2026-02-01'
    # Persists and is returned in the list.
    got = client.get(f"{BASE}/applications/{a['id']}", headers=headers)
    assert got.status_code == 200 and got.json()['company'] == 'Acme'
    assert len(client.get(f'{BASE}/applications', headers=headers).json()) == 1


def test_defaults_status_saved(client):
    headers = signup(client)
    r = client.post(f'{BASE}/applications', headers=headers, json={'company': 'Beta', 'jobTitle': 'Dev'})
    assert r.status_code == 201 and r.json()['status'] == 'Saved'


def test_update_fields_and_status_transition(client):
    headers = signup(client)
    aid = client.post(f'{BASE}/applications', headers=headers,
                      json={'company': 'Acme', 'jobTitle': 'Eng', 'status': 'Applied'}).json()['id']
    r = client.patch(f'{BASE}/applications/{aid}', headers=headers,
                     json={'status': 'Interview', 'notes': 'Call scheduled', 'followUpDate': '2026-03-01'})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'Interview' and r.json()['notes'] == 'Call scheduled'
    # Other fields untouched by a partial update.
    assert r.json()['company'] == 'Acme'


def test_delete(client):
    headers = signup(client)
    aid = client.post(f'{BASE}/applications', headers=headers,
                      json={'company': 'Acme', 'jobTitle': 'Eng'}).json()['id']
    assert client.delete(f'{BASE}/applications/{aid}', headers=headers).status_code == 204
    assert client.get(f'{BASE}/applications/{aid}', headers=headers).status_code == 404
    assert client.get(f'{BASE}/applications', headers=headers).json() == []


def test_search_filter_sort_paginate(client):
    headers = signup(client)
    for company, title, status, date in [
        ('Acme', 'Backend Engineer', 'Applied', '2026-01-01'),
        ('Globex', 'Frontend Engineer', 'Interview', '2026-02-01'),
        ('Initech', 'Designer', 'Saved', '2026-03-01'),
    ]:
        client.post(f'{BASE}/applications', headers=headers,
                    json={'company': company, 'jobTitle': title, 'status': status, 'dateApplied': date})
    # filter by status
    r = client.get(f'{BASE}/applications?status=Interview', headers=headers).json()
    assert len(r) == 1 and r[0]['company'] == 'Globex'
    # search company/title (case-insensitive)
    r = client.get(f'{BASE}/applications?q=engineer', headers=headers).json()
    assert {x['company'] for x in r} == {'Acme', 'Globex'}
    # sort by dateApplied asc
    r = client.get(f'{BASE}/applications?sort=dateApplied&order=asc', headers=headers).json()
    assert [x['company'] for x in r] == ['Acme', 'Globex', 'Initech']
    # pagination
    r = client.get(f'{BASE}/applications?sort=dateApplied&order=asc&limit=1&offset=1', headers=headers).json()
    assert len(r) == 1 and r[0]['company'] == 'Globex'


def test_validation_errors(client):
    headers = signup(client)
    assert client.post(f'{BASE}/applications', headers=headers, json={'company': '', 'jobTitle': 'X'}).status_code == 422
    assert client.post(f'{BASE}/applications', headers=headers, json={'company': 'X'}).status_code == 422  # missing jobTitle
    assert client.post(f'{BASE}/applications', headers=headers,
                       json={'company': 'X', 'jobTitle': 'Y', 'status': 'Bogus'}).status_code == 422
    assert client.post(f'{BASE}/applications', headers=headers,
                       json={'company': 'X', 'jobTitle': 'Y', 'jobUrl': 'not a url'}).status_code == 422
    assert client.post(f'{BASE}/applications', headers=headers,
                       json={'company': 'X', 'jobTitle': 'Y', 'dateApplied': '2999-01-01'}).status_code == 422


def test_cross_user_isolation(client):
    ana = signup(client, 'ana@example.com')
    aid = client.post(f'{BASE}/applications', headers=ana, json={'company': 'Acme', 'jobTitle': 'Eng'}).json()['id']
    bob = signup(client, 'bob@example.com')
    assert client.get(f'{BASE}/applications/{aid}', headers=bob).status_code == 404
    assert client.patch(f'{BASE}/applications/{aid}', headers=bob, json={'status': 'Offer'}).status_code == 404
    assert client.delete(f'{BASE}/applications/{aid}', headers=bob).status_code == 404
    assert client.get(f'{BASE}/applications', headers=bob).json() == []


def test_link_valid_cv_and_reject_foreign_cv(client):
    ana = signup(client, 'ana@example.com')
    ana_cv = make_cv(client, ana)
    r = client.post(f'{BASE}/applications', headers=ana,
                    json={'company': 'Acme', 'jobTitle': 'Eng', 'cvId': ana_cv})
    assert r.status_code == 201 and r.json()['cvId'] == ana_cv
    # Another user's CV cannot be linked.
    bob = signup(client, 'bob@example.com')
    bob_cv = make_cv(client, bob)
    assert client.post(f'{BASE}/applications', headers=ana,
                       json={'company': 'X', 'jobTitle': 'Y', 'cvId': bob_cv}).status_code == 422
    # A non-existent / malformed CV id is rejected too.
    assert client.post(f'{BASE}/applications', headers=ana,
                       json={'company': 'X', 'jobTitle': 'Y', 'cvId': 'not-an-id'}).status_code == 422
    # Unlinking via empty string works.
    aid = r.json()['id']
    assert client.patch(f'{BASE}/applications/{aid}', headers=ana, json={'cvId': ''}).json()['cvId'] is None


def test_invalid_application_id_is_404(client):
    headers = signup(client)
    assert client.get(f'{BASE}/applications/not-an-id', headers=headers).status_code == 404
    assert client.patch(f'{BASE}/applications/not-an-id', headers=headers, json={'status': 'Offer'}).status_code == 404
    assert client.delete(f'{BASE}/applications/not-an-id', headers=headers).status_code == 404


@pytest.mark.parametrize(('field', 'value'), [
    ('status', None), ('company', None), ('jobTitle', None),
    ('status', 'Bogus'), ('company', '  '), ('jobTitle', '\t'),
])
def test_patch_rejects_invalid_required_values_without_changing_the_record(client, field, value):
    headers = signup(client)
    saved = client.post(f'{BASE}/applications', headers=headers, json={
        'company': 'Acme', 'jobTitle': 'Engineer', 'status': 'Applied', 'notes': 'Keep these notes',
    }).json()
    url = f"{BASE}/applications/{saved['id']}"
    response = client.patch(url, headers=headers, json={field: value, 'notes': 'Do not write this'})
    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['body', field]
    got = client.get(url, headers=headers).json()
    assert got['notes'] == saved['notes']
    assert all(got[key] == saved[key] for key in ('company', 'jobTitle', 'status'))


def test_patch_explicit_null_clears_optional_fields_but_omission_leaves_them_intact(client):
    headers = signup(client)
    cv = make_cv(client, headers)
    optional = {
        'jobUrl': 'example.com/jobs/1', 'location': 'Remote', 'workArrangement': 'Remote',
        'employmentType': 'Full-time', 'dateApplied': '2025-01-10', 'notes': 'Referral',
        'followUpDate': '2025-02-10', 'cvId': cv,
    }
    saved = client.post(f'{BASE}/applications', headers=headers, json={
        'company': 'Acme', 'jobTitle': 'Engineer', 'status': 'Applied', **optional,
    }).json()
    url = f"{BASE}/applications/{saved['id']}"
    partial = client.patch(url, headers=headers, json={'jobTitle': ' Senior Engineer '})
    assert partial.status_code == 200
    assert partial.json()['jobTitle'] == 'Senior Engineer'
    assert {key: partial.json()[key] for key in optional} == optional

    cleared = client.patch(url, headers=headers, json=dict.fromkeys(optional))
    assert cleared.status_code == 200
    assert {key: cleared.json()[key] for key in optional} == dict.fromkeys(optional)
    assert cleared.json()['company'] == 'Acme' and cleared.json()['status'] == 'Applied'
    assert client.patch(url, headers=headers, json={}).json() == cleared.json()


def test_deleting_a_cv_preserves_the_application_and_allows_notes_replacement_and_unlink(client):
    headers = signup(client)
    cv_id = make_cv(client, headers)
    fields = {
        'company': 'Historical Employer', 'jobTitle': 'Engineer', 'status': 'Interview',
        'jobUrl': 'example.com/jobs/one', 'dateApplied': '2025-01-10',
        'followUpDate': '2025-02-01', 'notes': 'First interview', 'cvId': cv_id,
    }
    saved = client.post(f'{BASE}/applications', headers=headers, json=fields)
    assert saved.status_code == 201
    app_id = saved.json()['id']
    url = f'{BASE}/applications/{app_id}'
    before_delete = client.get(url, headers=headers).json()

    assert client.delete(f'{BASE}/cvs/{cv_id}', headers=headers).status_code == 204
    assert client.get(f'{BASE}/cvs/{cv_id}', headers=headers).status_code == 404
    assert client.get(url, headers=headers).json() == before_delete
    assert client.get(f'{BASE}/applications', headers=headers).json() == [before_delete]

    note = 'Second interview'
    edited = client.patch(url, headers=headers, json={'notes': note})
    assert edited.status_code == 200
    assert {key: edited.json()[key] for key in fields} == {**fields, 'notes': note}

    other = signup(client, 'other@example.com')
    foreign_cv = make_cv(client, other)
    for unavailable in (cv_id, foreign_cv, 'not-an-id', str(PydanticObjectId())):
        rejected = client.patch(url, headers=headers, json={'cvId': unavailable, 'notes': 'Do not write'})
        assert rejected.status_code == 422 and rejected.json() == {'detail': 'Linked CV not found'}
        assert client.get(url, headers=headers).json()['notes'] == note

    replacement = make_cv(client, headers, 'Replacement')
    linked = client.patch(url, headers=headers, json={'cvId': replacement})
    assert linked.status_code == 200 and linked.json()['cvId'] == replacement
    unlinked = client.patch(url, headers=headers, json={'cvId': None})
    assert unlinked.status_code == 200 and unlinked.json()['cvId'] is None
    assert client.get(f'{BASE}/cvs/{replacement}', headers=headers).status_code == 200
    assert client.patch(url, headers=other, json={'cvId': foreign_cv}).status_code == 404


def test_legacy_dangling_link_and_match_snapshot_survive_unrelated_patch(client):
    headers = signup(client)
    saved = client.post(f'{BASE}/applications', headers=headers, json={
        'company': 'Archived Co', 'jobTitle': 'Analyst',
    }).json()
    oid = PydanticObjectId(saved['id'])
    missing_cv = PydanticObjectId()
    snapshot = {'jobTitle': 'Analyst', 'company': 'Archived Co', 'score': 83}
    collection = Application.get_pymongo_collection()

    async def seed():
        await collection.update_one({'_id': oid}, {'$set': {'cv_id': missing_cv, 'match_snapshot': snapshot}})

    async def read():
        return await collection.find_one({'_id': oid})

    client.portal.call(seed)
    url = f"{BASE}/applications/{saved['id']}"
    assert client.get(url, headers=headers).json()['cvId'] == str(missing_cv)
    updated = client.patch(url, headers=headers, json={'notes': 'Kept for history'})
    assert updated.status_code == 200 and updated.json()['cvId'] == str(missing_cv)
    assert client.portal.call(read)['match_snapshot'] == snapshot
    assert client.patch(url, headers=headers, json={'cvId': None}).json()['cvId'] is None
    assert client.portal.call(read)['match_snapshot'] == snapshot

    assert client.patch(f'{BASE}/applications/{PydanticObjectId()}', headers=headers,
                        json={'cvId': str(missing_cv)}).status_code == 404


def test_concurrent_partial_updates_do_not_overwrite_each_other(client, monkeypatch):
    headers = signup(client)
    saved = client.post(f'{BASE}/applications', headers=headers, json={
        'company': 'Acme', 'jobTitle': 'Engineer',
    }).json()
    url = f"{BASE}/applications/{saved['id']}"
    real_owned = application_api.owned
    both_read = asyncio.Event()
    reads = 0

    async def read_together(*args):
        nonlocal reads
        app = await real_owned(*args)
        reads += 1
        if reads == 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), timeout=5)
        return app

    with monkeypatch.context() as patch:
        patch.setattr(application_api, 'owned', read_together)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.patch, url, headers=headers, json={'status': 'Interview'})
            second = pool.submit(client.patch, url, headers=headers, json={'notes': 'Call scheduled'})
            assert first.result(timeout=10).status_code == second.result(timeout=10).status_code == 200

    final = client.get(url, headers=headers).json()
    assert final['status'] == 'Interview' and final['notes'] == 'Call scheduled'
