"""Contract tests for the React UI endpoints (requires a test MongoDB)."""

BASE = '/api/v1'


def signup(client, email='jane@example.com'):
    result = client.post(f'{BASE}/auth/register', json={
        'name': 'Jane Doe', 'email': email, 'password': 'secret123',
    })
    assert result.status_code == 201, result.text
    assert result.json()['user']['name'] == 'Jane Doe'
    assert 'flowjob_refresh' in client.cookies
    return {'Authorization': f"Bearer {result.json()['accessToken']}"}


def test_register_refresh_and_logout(client):
    signup(client)
    res = client.post(f'{BASE}/auth/refresh')
    assert res.status_code == 200
    assert res.json()['user']['email'] == 'jane@example.com'
    assert client.post(f'{BASE}/auth/logout').status_code == 204
    assert client.post(f'{BASE}/auth/refresh').status_code == 401


def test_profile_round_trip_and_native_pdf_profile(client):
    headers = signup(client)
    body = client.get(f'{BASE}/profile', headers=headers).json()
    body.update({'summary': 'Backend engineer', 'skills': ['Python'], 'languages': 'English C1',
                 'experience': [{'id': 'exp-1', 'title': 'Developer', 'company': 'Acme',
                                 'start': '2024-05', 'end': '', 'bullets': ['Built APIs']}]})
    res = client.put(f'{BASE}/profile', json=body, headers=headers)
    assert res.status_code == 200, res.text
    assert client.get(f'{BASE}/profile', headers=headers).json() == body
    native = client.get('/profile', headers=headers).json()
    assert native['experience'][0]['position'] == 'Developer'
    assert native['skills'] == ['Python']


def test_cv_crud_and_user_isolation(client):
    jane = signup(client)
    res = client.post(f'{BASE}/cvs', json={'name': 'My CV', 'kind': 'original'}, headers=jane)
    assert res.status_code == 201, res.text
    cv = res.json()
    assert cv['name'] == 'My CV' and cv['content']['fullName'] == 'Jane Doe'
    cv_id = cv['id']
    assert len(client.get(f'{BASE}/cvs', headers=jane).json()) == 1
    patched = client.put(f'{BASE}/cvs/{cv_id}', json={'name': 'Updated'}, headers=jane)
    assert patched.status_code == 200 and patched.json()['name'] == 'Updated'
    other = signup(client, 'other@example.com')
    assert client.get(f'{BASE}/cvs/{cv_id}', headers=other).status_code == 404
    assert client.delete(f'{BASE}/cvs/{cv_id}', headers=jane).status_code == 204
    assert client.get(f'{BASE}/cvs', headers=jane).json() == []


def test_missing_features_are_not_mocked(client):
    headers = signup(client)
    # Gmail is real but reports itself unavailable until Google OAuth is configured; nothing is faked.
    gmail = client.get(f'{BASE}/gmail', headers=headers).json()
    assert gmail['available'] is False and gmail['connection'] == {'state': 'not_connected'}
    assert client.post(f'{BASE}/tailor', headers=headers, json={'matchId': 'x'}).status_code == 404
