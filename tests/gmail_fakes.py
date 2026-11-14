"""Deterministic Google/Gmail fakes and helpers for the Gmail tests. Nothing here touches the network."""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import settings
from app.gmail import google as google_module
from app.gmail import sync as sync_module
from app.gmail.google import (
    GMAIL_SCOPE, GoogleUnavailable, Identity, IdTokenInvalid, InvalidGrant, MessageGone, TokenResponse,
)

BASE = '/api/v1'


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii').rstrip('=')


def make_message(mid: str, *, subject: str, body: str, sender: str = 'Talent Team <jobs@acme.example>',
                 at: datetime | None = None, thread: str | None = None, html: bool = False,
                 attachment: bool = False, labels: list[str] | None = None) -> dict:
    at = at or datetime.now(UTC)
    part = {'mimeType': 'text/html' if html else 'text/plain', 'headers': [
        {'name': 'Content-Type', 'value': ('text/html' if html else 'text/plain') + '; charset=utf-8'}],
        'body': {'data': b64(body), 'size': len(body)}}
    parts = [part]
    if attachment:
        parts.append({'mimeType': 'application/pdf', 'filename': 'offer.pdf', 'headers': [],
                      'body': {'attachmentId': 'ATT-SECRET', 'size': 9000}})
    headers = [{'name': 'From', 'value': sender}, {'name': 'Subject', 'value': subject}]
    return {
        'id': mid, 'threadId': thread or f't{mid}', 'internalDate': str(int(at.timestamp() * 1000)),
        'labelIds': labels or ['INBOX'], 'snippet': body[:120],
        'payload': {'mimeType': 'multipart/mixed', 'headers': headers, 'parts': parts},
    }


class FakeGoogle:
    def __init__(self) -> None:
        self.identity = Identity(sub='google-sub-1', email='student@gmail.example')
        self.scopes = ['openid', 'email', GMAIL_SCOPE]
        self.refresh_token: str | None = 'refresh-token-1'
        self.exchanged: list[tuple[str, str]] = []
        self.nonces: list[str] = []
        self.revoked: list[str] = []
        self.revoke_result = True
        self.revoke_error: Exception | None = None
        self.refreshed: list[str] = []
        self.refresh_error: Exception | None = None
        self.id_error = False
        self.exchange_error: Exception | None = None
        self.messages: dict[str, dict] = {}
        self.queries: list[str] = []
        self.page_size = 100
        self.list_error: Exception | None = None
        self.fail_list_after_pages: int | None = None
        self.get_errors: dict[str, Exception] = {}
        self.full_fetches: list[str] = []
        self.metadata_fetches: list[str] = []
        self.list_calls = 0

    async def exchange_code(self, code: str, verifier: str) -> TokenResponse:
        if self.exchange_error:
            raise self.exchange_error
        self.exchanged.append((code, verifier))
        return TokenResponse(access_token=f'access-{code}', refresh_token=self.refresh_token,
                             scopes=list(self.scopes), id_token=f'id-{code}')

    async def verify_id_token(self, token: str, nonce: str) -> Identity:
        self.nonces.append(nonce)
        if self.id_error:
            raise IdTokenInvalid()
        return self.identity

    async def refresh(self, refresh_token: str) -> TokenResponse:
        self.refreshed.append(refresh_token)
        if self.refresh_error:
            raise self.refresh_error
        return TokenResponse(access_token='access-fresh', refresh_token=None, scopes=list(self.scopes), id_token=None)

    async def revoke(self, token: str) -> bool:
        self.revoked.append(token)
        if self.revoke_error:
            raise self.revoke_error
        return self.revoke_result

    def _window(self, query: str) -> tuple[int, int]:
        after = before = None
        for term in query.split():
            if term.startswith('after:'):
                after = int(term[6:])
            if term.startswith('before:'):
                before = int(term[7:])
        return after or 0, before or 10**12

    async def list_messages(self, access_token: str, query: str, page_token: str | None) -> dict:
        self.list_calls += 1
        if self.list_error:
            raise self.list_error
        page = int(page_token or 0)
        if self.fail_list_after_pages is not None and page >= self.fail_list_after_pages:
            raise GoogleUnavailable()
        self.queries.append(query)
        after, before = self._window(query)
        ids = sorted(
            (m for m in self.messages.values()
             if after <= int(m['internalDate']) // 1000 < before and not {'SPAM', 'TRASH'} & set(m['labelIds'])),
            key=lambda m: -int(m['internalDate']),
        )
        chunk = ids[page * self.page_size:(page + 1) * self.page_size]
        out: dict = {'messages': [{'id': m['id'], 'threadId': m['threadId']} for m in chunk]}
        if (page + 1) * self.page_size < len(ids):
            out['nextPageToken'] = str(page + 1)
        return out

    def _get(self, message_id: str) -> dict:
        if message_id in self.get_errors:
            raise self.get_errors[message_id]
        if message_id not in self.messages:
            raise MessageGone()
        return self.messages[message_id]

    async def get_metadata(self, access_token: str, message_id: str) -> dict:
        self.metadata_fetches.append(message_id)
        m = self._get(message_id)
        return {k: v for k, v in m.items() if k != 'payload'} | {'payload': {'headers': m['payload']['headers']}}

    async def get_full(self, access_token: str, message_id: str) -> dict:
        self.full_fetches.append(message_id)
        return self._get(message_id)


@pytest.fixture
def fake_google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(google_module, 'google', fake)
    monkeypatch.setattr(settings, 'google_oauth_client_id', 'test-client.apps.example')
    monkeypatch.setattr(settings, 'google_oauth_client_secret', 'test-client-secret')
    monkeypatch.setattr(settings, 'gmail_token_key', base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(settings, 'gmail_token_keys_old', '')
    monkeypatch.setattr(settings, 'frontend_base_url', 'http://localhost:5173')

    async def inline(coro):
        await coro
    monkeypatch.setattr(sync_module, 'spawn', inline)
    return fake


def signup(client, email='ana@example.com', name='Ana Nowak'):
    client.cookies.clear()
    r = client.post(f'{BASE}/auth/register', json={'name': name, 'email': email, 'password': 'secret123'})
    assert r.status_code == 201, r.text
    return {'Authorization': f"Bearer {r.json()['accessToken']}"}


def start_flow(client, headers, mode='connect', timezone='Europe/Warsaw'):
    client.cookies.clear()
    r = client.post(f'{BASE}/gmail/oauth/start', headers=headers, json={'mode': mode, 'timezone': timezone})
    assert r.status_code == 200, r.text
    query = parse_qs(urlparse(r.json()['authorizationUrl']).query)
    return {k: v[0] for k, v in query.items()}, client.cookies.get('flowjob_gmail_oauth')


def callback(client, state, code='auth-code', cookie=None, **extra):
    client.cookies.clear()
    if cookie is not None:
        client.cookies.set('flowjob_gmail_oauth', cookie, path='/api/v1/gmail/oauth')
    params = {'state': state, 'code': code, **extra}
    r = client.get(f'{BASE}/gmail/oauth/callback', params=params, follow_redirects=False)
    assert r.status_code == 303, r.text
    return parse_qs(urlparse(r.headers['location']).query)


def connect(client, headers, **kw):
    params, cookie = start_flow(client, headers, **kw)
    result = callback(client, params['state'], cookie=cookie)
    assert result.get('gmail') == ['connected'], result
    return params
