"""Checkpoint 1: Gmail OAuth, encrypted credentials, ownership and disconnect semantics (all mocked)."""
import base64
import logging
import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.applications.models import Application, ApplicationEvent
from app.config import settings
from app.gmail import crypto
from app.gmail.google import GoogleApi, IdTokenInvalid, InvalidGrant, pkce_challenge
from app.gmail.models import GmailConnection, GmailMessage, GmailOAuthState, GmailReview, GmailSuppression
from tests.gmail_fakes import BASE, callback, connect, fake_google, signup, start_flow  # noqa: F401


def run(client, coro):
    async def wrapper():
        return await coro
    return client.portal.call(wrapper)


def conn_of(client, headers):
    me = client.get(f'{BASE}/gmail', headers=headers).json()
    return me


# --- configuration ------------------------------------------------------------------------------------------

def test_unconfigured_gmail_is_unavailable_but_app_works(client):
    headers = signup(client)
    status = client.get(f'{BASE}/gmail', headers=headers).json()
    assert status['available'] is False and status['connection'] == {'state': 'not_connected'}
    assert client.post(f'{BASE}/gmail/oauth/start', headers=headers, json={}).status_code == 503
    assert client.get(f'{BASE}/applications', headers=headers).status_code == 200


def test_endpoints_require_authentication(client, fake_google):
    assert client.get(f'{BASE}/gmail').status_code == 401
    assert client.post(f'{BASE}/gmail/oauth/start', json={}).status_code == 401
    assert client.delete(f'{BASE}/gmail').status_code == 401


# --- start / authorization URL ------------------------------------------------------------------------------

def test_start_builds_pkce_url_and_binding_cookie(client, fake_google):
    headers = signup(client)
    r = client.post(f'{BASE}/gmail/oauth/start', headers=headers, json={'mode': 'connect', 'timezone': 'Europe/Warsaw'})
    assert r.status_code == 200
    cookie_header = r.headers['set-cookie']
    assert 'flowjob_gmail_oauth=' in cookie_header and 'HttpOnly' in cookie_header
    assert 'Path=/api/v1/gmail/oauth' in cookie_header and 'SameSite=lax' in cookie_header
    params, cookie = start_flow(client, headers)
    assert params['code_challenge_method'] == 'S256' and len(params['code_challenge']) == 43
    assert params['scope'] == 'openid email https://www.googleapis.com/auth/gmail.readonly'
    assert params['access_type'] == 'offline' and params['redirect_uri'] == settings.google_oauth_redirect_uri
    assert len(params['state']) >= 40 and params['nonce']
    # The state is stored hashed; neither the state nor the verifier appears in clear text.
    stored = run(client, GmailOAuthState.find_all().to_list())
    assert stored and all(params['state'] not in s.state_hash and s.enc_verifier.startswith('v1.') for s in stored)


def test_pkce_verifier_matches_challenge_and_nonce_is_checked(client, fake_google):
    headers = signup(client)
    params, cookie = start_flow(client, headers)
    assert callback(client, params['state'], cookie=cookie)['gmail'] == ['connected']
    code, verifier = fake_google.exchanged[-1]
    assert pkce_challenge(verifier) == params['code_challenge']
    assert fake_google.nonces[-1] == params['nonce']


# --- state and browser binding ------------------------------------------------------------------------------

def test_state_is_single_use(client, fake_google):
    headers = signup(client)
    params, cookie = start_flow(client, headers)
    assert callback(client, params['state'], cookie=cookie)['gmail'] == ['connected']
    again = callback(client, params['state'], cookie=cookie)
    assert again == {'gmail': ['error'], 'reason': ['invalid_state']}
    assert len(fake_google.exchanged) == 1


def test_expired_state_is_rejected(client, fake_google):
    headers = signup(client)
    params, cookie = start_flow(client, headers)
    run(client, GmailOAuthState.get_pymongo_collection().update_many(
        {}, {'$set': {'expires_at': datetime.now(UTC) - timedelta(seconds=1)}}))
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['invalid_state']
    assert fake_google.exchanged == []


@pytest.mark.parametrize('cookie', [None, 'forged-cookie-value'])
def test_missing_or_wrong_browser_binding_is_rejected(client, fake_google, cookie):
    headers = signup(client)
    params, _ = start_flow(client, headers)
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['invalid_state']
    assert fake_google.exchanged == [] and run(client, GmailConnection.find_all().count()) == 0


def test_unknown_state_and_denied_consent(client, fake_google):
    headers = signup(client)
    assert callback(client, 'not-a-real-state', cookie='x')['reason'] == ['invalid_state']
    params, cookie = start_flow(client, headers)
    assert callback(client, params['state'], cookie=cookie, error='access_denied')['reason'] == ['denied']


# --- identity and scope --------------------------------------------------------------------------------------

def test_invalid_identity_token_is_rejected(client, fake_google):
    headers = signup(client)
    fake_google.id_error = True
    params, cookie = start_flow(client, headers)
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['invalid_identity']
    assert run(client, GmailConnection.find_all().count()) == 0


def test_missing_gmail_permission_is_rejected(client, fake_google):
    headers = signup(client)
    fake_google.scopes = ['openid', 'email']
    params, cookie = start_flow(client, headers)
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['scope_missing']
    assert fake_google.revoked == []


def test_missing_refresh_token_on_first_connect(client, fake_google):
    headers = signup(client)
    fake_google.refresh_token = None
    params, cookie = start_flow(client, headers)
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['no_refresh_token']


def _rsa_token(claims, key):
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'k1'})


class _Jwks:
    def __init__(self, public):
        self.public = public

    def get_signing_key_from_jwt(self, token):
        return type('K', (), {'key': self.public})()


@pytest.mark.parametrize('change', ['ok', 'issuer', 'audience', 'expired', 'nonce', 'signature'])
def test_real_id_token_verification(fake_google, change):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    claims = {'iss': 'https://accounts.google.com', 'aud': settings.google_oauth_client_id, 'sub': '123',
              'email': 'Student@Gmail.example', 'iat': now, 'exp': now + timedelta(minutes=5), 'nonce': 'n1'}
    signer = key
    if change == 'issuer':
        claims['iss'] = 'https://evil.example'
    elif change == 'audience':
        claims['aud'] = 'someone-else'
    elif change == 'expired':
        claims['exp'] = now - timedelta(minutes=5)
    elif change == 'nonce':
        claims['nonce'] = 'other'
    elif change == 'signature':
        signer = other
    api = GoogleApi()
    api._jwks = _Jwks(key.public_key())
    token = _rsa_token(claims, signer)
    if change == 'ok':
        identity = api._verify_id_token_sync(token, 'n1')
        assert identity.sub == '123' and identity.email == 'student@gmail.example'
    else:
        with pytest.raises(IdTokenInvalid):
            api._verify_id_token_sync(token, 'n1')


# --- ownership ------------------------------------------------------------------------------------------------

def test_google_account_owned_by_another_user_is_refused_without_revoking(client, fake_google):
    first = signup(client, 'one@example.com')
    connect(client, first)
    second = signup(client, 'two@example.com')
    params, cookie = start_flow(client, second)
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['account_in_use']
    assert fake_google.revoked == []  # the first user's grant must survive
    assert client.get(f'{BASE}/gmail', headers=first).json()['connection']['state'] == 'active'
    assert client.get(f'{BASE}/gmail', headers=second).json()['connection']['state'] == 'not_connected'


def test_one_gmail_per_user(client, fake_google):
    headers = signup(client)
    connect(client, headers)
    assert client.post(f'{BASE}/gmail/oauth/start', headers=headers, json={'mode': 'connect'}).status_code == 409


def test_reconnect_with_a_different_account_is_refused(client, fake_google):
    headers = signup(client)
    connect(client, headers)
    fake_google.identity = type(fake_google.identity)(sub='google-sub-OTHER', email='other@gmail.example')
    params, cookie = start_flow(client, headers, mode='reconnect')
    assert params['login_hint'] == 'student@gmail.example'
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['different_account']
    conn = run(client, GmailConnection.find_one())
    assert conn.google_sub == 'google-sub-1'


def test_reconnect_requires_existing_connection(client, fake_google):
    headers = signup(client)
    assert client.post(f'{BASE}/gmail/oauth/start', headers=headers, json={'mode': 'reconnect'}).status_code == 409


# --- credentials ----------------------------------------------------------------------------------------------

def test_refresh_token_is_encrypted_and_bound_to_owner(client, fake_google):
    headers = signup(client)
    connect(client, headers)
    conn = run(client, GmailConnection.find_one())
    assert 'refresh-token-1' not in conn.enc_refresh_token
    aad = crypto.connection_aad(conn.user_id, conn.google_sub)
    assert crypto.decrypt(conn.enc_refresh_token, aad) == 'refresh-token-1'
    with pytest.raises(crypto.TokenCryptoError):
        crypto.decrypt(conn.enc_refresh_token, crypto.connection_aad('someone-else', conn.google_sub))
    with pytest.raises(crypto.TokenCryptoError):
        crypto.decrypt(conn.enc_refresh_token[:-4] + 'AAAA', aad)


def test_key_rotation_keeps_old_ciphertexts_readable(fake_google, monkeypatch):
    old = crypto.encrypt('secret', 'aad')
    monkeypatch.setattr(settings, 'gmail_token_keys_old', settings.gmail_token_key)
    monkeypatch.setattr(settings, 'gmail_token_key', base64.b64encode(os.urandom(32)).decode())
    assert crypto.decrypt(old, 'aad') == 'secret' and crypto.needs_rotation(old)
    new = crypto.encrypt('secret', 'aad')
    assert not crypto.needs_rotation(new) and crypto.decrypt(new, 'aad') == 'secret'
    monkeypatch.setattr(settings, 'gmail_token_keys_old', '')
    with pytest.raises(crypto.TokenCryptoError):
        crypto.decrypt(old, 'aad')


def test_tokens_never_appear_in_responses_or_logs(client, fake_google, caplog):
    caplog.set_level(logging.DEBUG)
    headers = signup(client)
    params, cookie = start_flow(client, headers)
    client.cookies.set('flowjob_gmail_oauth', cookie, path='/api/v1/gmail/oauth')
    r = client.get(f'{BASE}/gmail/oauth/callback', params={'state': params['state'], 'code': 'c1'},
                   follow_redirects=False)
    status = client.get(f'{BASE}/gmail', headers=headers)
    blob = r.text + str(r.headers) + status.text + caplog.text
    for secret in ('refresh-token-1', 'access-c1', 'id-c1', settings.google_oauth_client_secret):
        assert secret not in blob
    assert 'enc_refresh_token' not in status.text and 'google_sub' not in status.text


# --- status, isolation, reconnect vs disconnect --------------------------------------------------------------

def test_status_is_owner_scoped(client, fake_google):
    a = signup(client, 'a@example.com')
    connect(client, a)
    b = signup(client, 'b@example.com')
    assert client.get(f'{BASE}/gmail', headers=b).json()['connection']['state'] == 'not_connected'
    assert client.delete(f'{BASE}/gmail', headers=b).status_code == 404
    assert client.get(f'{BASE}/gmail', headers=a).json()['connection']['email'] == 'student@gmail.example'


def test_expired_grant_marks_reauth_and_reconnect_preserves_progress(client, fake_google):
    from app.gmail.connection import access_token
    headers = signup(client)
    connect(client, headers)
    conn = run(client, GmailConnection.find_one())
    run(client, GmailMessage(user_id=conn.user_id, connection_id=conn.id, message_id='m1',
                             internal_at=datetime.now(UTC), state='processed').insert())
    fake_google.refresh_error = InvalidGrant()
    with pytest.raises(InvalidGrant):
        run(client, access_token(conn))
    status = client.get(f'{BASE}/gmail', headers=headers).json()
    assert status['connection']['state'] == 'reauth_required'
    assert status['sync']['error']['code'] == 'reauth_required'

    fake_google.refresh_error = None
    fake_google.refresh_token = 'refresh-token-2'
    params, cookie = start_flow(client, headers, mode='reconnect')
    assert callback(client, params['state'], cookie=cookie)['gmail'] == ['reconnected']
    after = run(client, GmailConnection.find_one())
    assert after.id == conn.id and after.status == 'active' and after.attention is None
    assert crypto.decrypt(after.enc_refresh_token, crypto.connection_aad(after.user_id, after.google_sub)) == 'refresh-token-2'
    assert run(client, GmailMessage.find(GmailMessage.connection_id == conn.id).count()) == 1


def test_reconnect_without_new_refresh_token_keeps_working_grant(client, fake_google):
    headers = signup(client)
    connect(client, headers)
    fake_google.refresh_token = None
    params, cookie = start_flow(client, headers, mode='reconnect')
    assert callback(client, params['state'], cookie=cookie)['gmail'] == ['reconnected']
    fake_google.refresh_error = InvalidGrant()
    params, cookie = start_flow(client, headers, mode='reconnect')
    assert callback(client, params['state'], cookie=cookie)['reason'] == ['no_refresh_token']


def test_disconnect_erases_gmail_data_and_keeps_applications(client, fake_google):
    headers = signup(client)
    connect(client, headers)
    conn = run(client, GmailConnection.find_one())
    uid, cid = conn.user_id, conn.id
    now = datetime.now(UTC)

    async def seed():
        app = Application(user_id=uid, company='Acme', job_title='Analyst', status='Interview', source='gmail',
                          imported_at=now, gmail_connection_id=cid, gmail_source_key='k', gmail_message_id='m1',
                          gmail_thread_id='t1')
        await app.insert()
        manual = Application(user_id=uid, company='Beta', job_title='Dev')
        await manual.insert()
        review = GmailReview(user_id=uid, connection_id=cid, message_id='m2', item_key='i', kind='status_change',
                             application_id=manual.id, internal_at=now, excerpt='We would like to invite you')
        await review.insert()
        await Application.get_pymongo_collection().update_one({'_id': manual.id},
                                                               {'$set': {'suggestion_review_id': review.id}})
        await ApplicationEvent(user_id=uid, application_id=app.id, source='gmail', event_type='interview_invitation',
                               occurred_at=now, connection_id=cid, message_id='m1', item_key='i',
                               excerpt='Invitation text').insert()
        await ApplicationEvent(user_id=uid, application_id=app.id, source='gmail_approved', event_type='status_change',
                               occurred_at=now, status_from='Applied', status_to='Interview').insert()
        await GmailMessage(user_id=uid, connection_id=cid, message_id='m1', internal_at=now, state='processed').insert()
        await GmailSuppression(user_id=uid, connection_id=cid, message_ids=['m9']).insert()
        return app.id, manual.id
    app_id, manual_id = run(client, seed())

    assert client.delete(f'{BASE}/gmail', headers=headers).status_code == 204
    assert fake_google.revoked == ['refresh-token-1']
    assert run(client, GmailConnection.find_all().count()) == 0
    for model in (GmailMessage, GmailReview, GmailSuppression, GmailOAuthState):
        assert run(client, model.find_all().count()) == 0
    app = run(client, Application.get(app_id))
    assert app.source == 'gmail' and app.imported_at and app.status.value == 'Interview'
    assert app.gmail_message_id is None and app.gmail_thread_id is None and app.gmail_connection_id is None
    assert run(client, Application.get(manual_id)).suggestion_review_id is None
    events = run(client, ApplicationEvent.find_all().to_list())
    assert [(e.source, e.excerpt, e.message_id) for e in events] == [('gmail_approved', None, None)]
    status = client.get(f'{BASE}/gmail', headers=headers).json()
    assert status['connection']['state'] == 'not_connected' and status['pendingReviewCount'] == 0
    # A fresh connect afterwards is allowed again.
    connect(client, headers)


def test_concurrent_callbacks_cannot_both_claim_one_google_account(client, fake_google):
    from app.gmail import oauth
    one = signup(client, 'one@example.com')
    first, cookie_one = start_flow(client, one)
    two = signup(client, 'two@example.com')
    second, cookie_two = start_flow(client, two)

    async def race():
        import asyncio
        return await asyncio.gather(
            oauth.complete(code='c1', state=first['state'], error=None, cookie=cookie_one),
            oauth.complete(code='c2', state=second['state'], error=None, cookie=cookie_two),
            return_exceptions=True)
    results = run(client, race())
    assert sum(isinstance(r, oauth.CallbackResult) for r in results) == 1
    assert [r.code for r in results if isinstance(r, oauth.OAuthFailure)] == ['account_in_use']
    assert run(client, GmailConnection.find_all().count()) == 1
    assert fake_google.revoked == []
