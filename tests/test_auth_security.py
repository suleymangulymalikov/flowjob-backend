"""Browser and legacy auth security against the disposable MongoDB test instance."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from pwdlib import PasswordHash
from pymongo.errors import AutoReconnect

from app.auth import limits, security
from app.auth.models import AuthRateBucket, RefreshToken
from app.config import settings
from app.users.models import User
from tests.test_auth import USER, login, register

BROWSER = '/api/v1/auth'
EMAIL = 'jane@example.com'
PASSWORD = USER['password']
COOKIE = 'flowjob_refresh'


def browser_register(client, email=EMAIL, password=PASSWORD, origin=''):
    return client.post(f'{origin}{BROWSER}/register', json={'email': email, 'password': password, 'name': 'Jane Doe'})


def browser_login(client, email=EMAIL, password=PASSWORD):
    return client.post(f'{BROWSER}/login', json={'email': email, 'password': password})


def bearer(token):
    return {'Authorization': f'Bearer {token}'}


def test_browser_and_legacy_auth_share_accounts_and_keep_user_identity(client):
    first = browser_register(client)
    assert first.status_code == 201 and first.headers['Cache-Control'] == 'no-store'
    assert browser_login(client, password='wrong-password').json() == {'detail': 'Incorrect email or password'}
    assert browser_login(client, email='missing@example.com').json() == {'detail': 'Incorrect email or password'}
    signed_in = browser_login(client, email='JANE@example.com')
    assert signed_in.status_code == 200 and signed_in.json()['user']['id'] == first.json()['user']['id']
    legacy = login(client)
    assert legacy.status_code == 200
    second = register(client, email='other@example.com')
    other = login(client, email='other@example.com')
    assert second.status_code == 201 and other.status_code == 200
    assert client.get('/api/v1/profile', headers=bearer(legacy.json()['access_token'])).json()['email'] == EMAIL
    assert client.get('/users/me', headers=bearer(other.json()['access_token'])).json()['id'] == second.json()['id']
    assert client.get('/users/me', headers=bearer(signed_in.json()['accessToken'])).json()['id'] != second.json()['id']


def test_failed_attempts_share_normalized_account_limit_across_route_families(client):
    assert register(client).status_code == 201
    for attempt in range(limits.LOGIN_PER_ACCOUNT):
        response = (browser_login if attempt % 2 else login)(client, 'JANE@example.com', 'wrong-password')
        assert response.status_code == 401 and response.json() == {'detail': 'Incorrect email or password'}
    for sign_in in (login, browser_login):
        response = sign_in(client, EMAIL, PASSWORD)
        assert response.status_code == 429 and response.headers['Cache-Control'] == 'no-store'
        assert 1 <= int(response.headers['Retry-After']) <= limits.WINDOW_SECONDS
    assert browser_login(client, 'other@example.com').status_code == 401


def test_simultaneous_login_attempts_cannot_exceed_shared_account_limit(client, monkeypatch):
    monkeypatch.setattr(limits, 'LOGIN_PER_ACCOUNT', 3)
    assert register(client).status_code == 201

    def sign_in(index):
        return (browser_login if index % 2 else login)(client, EMAIL, 'wrong-password').status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(sign_in, range(8)))
    assert sorted(statuses) == [401] * 3 + [429] * 5


def test_rate_limit_failure_fails_closed_before_password_work(client, monkeypatch):
    collection_type = type(AuthRateBucket.get_pymongo_collection())

    async def unavailable(*args, **kwargs):
        raise AutoReconnect('private connection fixture')

    def unexpected(*args):
        pytest.fail('Password work began without a rate-limit decision.')

    monkeypatch.setattr(collection_type, 'find_one_and_update', unavailable)
    monkeypatch.setattr(security.password_hash, 'hash', unexpected)
    monkeypatch.setattr(security.password_hash, 'verify', unexpected)
    for response in (browser_login(client), register(client)):
        assert response.status_code == 503
        assert response.json() == {'detail': 'Authentication is temporarily unavailable. Please try again.'}


def test_ip_limit_covers_different_accounts_and_ignores_spoofed_headers(client, monkeypatch):
    monkeypatch.setattr(limits, 'LOGIN_PER_NETWORK', 2)
    assert browser_login(client, 'one@example.com').status_code == 401
    assert login(client, 'two@example.com').status_code == 401
    response = client.post('/auth/login', data={'username': 'three@example.com', 'password': PASSWORD},
                           headers={'X-Forwarded-For': '203.0.113.15'})
    assert response.status_code == 429


def test_registration_limit_is_shared_and_atomic_for_concurrent_requests(client, monkeypatch):
    monkeypatch.setattr(limits, 'REGISTER_PER_NETWORK', 4)

    def submit(index):
        email = f'candidate-{index}@example.com'
        if index % 2:
            return browser_register(client, email).status_code
        return register(client, email=email).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(submit, range(8)))
    assert sorted(statuses) == [201] * 4 + [429] * 4

    async def rate_rows():
        return await AuthRateBucket.get_pymongo_collection().find().to_list()

    rows = client.portal.call(rate_rows)
    assert len(rows) == 1 and rows[0]['attempts'] == 4
    assert set(rows[0]) == {'_id', 'attempts', 'expires_at'}
    assert len(rows[0]['_id']) == 64  # no raw email or IP in the rate-limit document


def test_auth_fixed_window_rollover_allows_new_attempts(client, monkeypatch):
    window = [limits.WINDOW_SECONDS * 2_100_000]
    monkeypatch.setattr(limits, 'time', lambda: window[0])
    monkeypatch.setattr(limits, 'LOGIN_PER_ACCOUNT', 1)
    assert browser_login(client, EMAIL).status_code == 401
    window[0] += limits.WINDOW_SECONDS - 1
    blocked = login(client, EMAIL)
    assert blocked.status_code == 429 and blocked.headers['Retry-After'] == '1'
    window[0] += 1
    assert login(client, EMAIL).status_code == 401


@pytest.mark.parametrize('invalid', ['tiny', 'private-fixture' + '\x00', '🙂' * 257, '\ud800' * 8])
def test_invalid_new_password_never_reaches_hasher_or_appears_in_error(client, monkeypatch, invalid):
    def unexpected(*args):
        pytest.fail('Invalid password reached Argon2.')

    monkeypatch.setattr(security.password_hash, 'hash', unexpected)
    for path, name_key in ((f'{BROWSER}/register', 'name'), ('/auth/register', 'full_name')):
        body = json.dumps({'email': EMAIL, 'password': invalid, name_key: 'Jane Doe'})
        response = client.post(path, content=body, headers={'Content-Type': 'application/json'})
        assert response.status_code == 422 and response.headers['Cache-Control'] == 'no-store'
        assert 'input' not in response.json()['detail'][0]
        assert 'private-fixture' not in response.text


def test_password_and_body_limits_apply_before_verification_on_both_login_routes(client, monkeypatch):
    def unexpected(*args):
        pytest.fail('Oversized credentials reached Argon2.')

    monkeypatch.setattr(security.password_hash, 'verify', unexpected)
    password = 'private-fixture' + 'x' * 1024
    for sign_in in (login, browser_login):
        response = sign_in(client, EMAIL, password)
        assert response.status_code == 422 and 'private-fixture' not in response.text

    body = b'username=jane%40example.com&password=' + b'x' * 17000
    for payload in (body, iter((body[:9000], body[9000:]))):
        response = client.post('/auth/login', content=payload,
                               headers={'Content-Type': 'application/x-www-form-urlencoded'})
        assert response.status_code == 413

    assert browser_login(client, EMAIL, 'x' * 17000).status_code == 413


def test_existing_argon2_hash_and_legacy_password_format_are_unchanged(client):
    password = '旧 passphrase\n' + 'x' * 290
    old_hash = PasswordHash.recommended().hash(password)

    async def seed():
        await User(email=EMAIL, full_name='Jane Doe', hashed_password=old_hash).insert()

    async def check():
        return (await User.find_one(User.email == EMAIL)).hashed_password

    client.portal.call(seed)
    assert browser_login(client, EMAIL, password).status_code == 200
    assert login(client, EMAIL, password).status_code == 200
    assert client.portal.call(check) == old_hash


def test_password_work_runs_outside_event_loop_and_unknown_users_verify_a_dummy_hash(client, monkeypatch):
    real_hash = security.password_hash.hash
    real_verify = security.password_hash.verify
    event_loop = client.portal.call(get_ident)
    entered, release = Event(), Event()
    seen = []

    def hold_hash(password):
        seen.append(get_ident())
        entered.set()
        assert release.wait(5)
        return real_hash(password)

    def observe_verify(password, encoded):
        seen.append(get_ident())
        assert encoded == security._DUMMY_HASH
        return real_verify(password, encoded)

    monkeypatch.setattr(security.password_hash, 'hash', hold_hash)
    monkeypatch.setattr(security.password_hash, 'verify', observe_verify)
    with ThreadPoolExecutor(max_workers=1) as pool:
        creating = pool.submit(browser_register, client)
        try:
            assert entered.wait(3)
            assert client.get('/health').status_code == 200
        finally:
            release.set()
        assert creating.result(timeout=5).status_code == 201
    assert browser_login(client, 'unknown@example.com').status_code == 401
    assert len(seen) == 2 and event_loop not in seen


@pytest.mark.parametrize('origin,force_secure,expected_secure', [
    ('http://localhost', False, False),
    ('https://testserver', False, True),
    ('http://testserver', True, True),
])
def test_cookie_security_on_localhost_https_and_explicit_production_override(
    client, monkeypatch, origin, force_secure, expected_secure,
):
    monkeypatch.setattr(settings, 'cookie_secure', force_secure)
    created = browser_register(client, origin=origin)
    assert created.status_code == 201
    cookie = created.headers['Set-Cookie']
    assert ('; Secure' in cookie) is expected_secure
    for attribute in ('; HttpOnly', '; Path=/api/v1/auth', '; SameSite=lax'):
        assert attribute in cookie

    if origin == 'http://localhost' or origin.startswith('https'):
        response = client.post(f'{origin}{BROWSER}/refresh')
        assert response.status_code == 200
        assert ('; Secure' in response.headers['Set-Cookie']) is expected_secure
    deleted = client.post(f'{origin}{BROWSER}/logout')
    assert deleted.status_code == 204
    assert ('; Secure' in deleted.headers['Set-Cookie']) is expected_secure
    assert 'Max-Age=0' in deleted.headers['Set-Cookie']
    assert client.post(f'{origin}{BROWSER}/refresh').status_code == 401


def test_concurrent_cross_family_refresh_has_one_winner_and_replay_cannot_clear_new_cookie(client):
    assert browser_register(client).status_code == 201
    cookie = client.cookies.get(COOKIE)
    assert cookie
    # Both calls try the identical original token even if the browser jar changes.
    with ThreadPoolExecutor(max_workers=2) as pool:
        legacy = pool.submit(client.post, '/auth/refresh', json={'refresh_token': cookie})
        browser = pool.submit(client.post, f'{BROWSER}/refresh', headers={'Cookie': f'{COOKIE}={cookie}'})
        left, right = legacy.result(timeout=5), browser.result(timeout=5)
    assert sorted([left.status_code, right.status_code]) == [200, 401]
    replay = client.post(f'{BROWSER}/refresh', headers={'Cookie': f'{COOKIE}={cookie}'})
    assert replay.status_code == 401 and 'Set-Cookie' not in replay.headers
    if right.status_code == 200:
        assert client.post(f'{BROWSER}/refresh').status_code == 200
    else:
        replacement = left.json()['refresh_token']
        assert client.post('/auth/refresh', json={'refresh_token': replacement}).status_code == 200


def test_expired_access_is_rejected_and_logout_revokes_the_rotated_cookie(client, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(settings, 'access_token_expire_minutes', -1)
        signup = browser_register(client)
    assert signup.status_code == 201
    assert client.get('/api/v1/profile', headers=bearer(signup.json()['accessToken'])).status_code == 401
    refreshed = client.post(f'{BROWSER}/refresh')
    assert refreshed.status_code == 200
    assert client.get('/api/v1/profile', headers=bearer(refreshed.json()['accessToken'])).status_code == 200
    rotated = client.cookies.get(COOKIE)
    assert client.post(f'{BROWSER}/logout').status_code == 204
    assert client.post(f'{BROWSER}/refresh', headers={'Cookie': f'{COOKIE}={rotated}'}).status_code == 401

    async def active_tokens():
        return await RefreshToken.get_pymongo_collection().count_documents({})

    assert client.portal.call(active_tokens) == 0
