"""Job application tracking API (authenticated, owner-scoped, MongoDB-backed)."""

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
