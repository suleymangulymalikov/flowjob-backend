"""Google OAuth 2.0 / OpenID Connect and Gmail REST calls over httpx.

The whole module surface is the ``GoogleApi`` instance ``google``; tests replace it with a deterministic fake,
so no test ever reaches Google. Errors are mapped to small exception types that never carry tokens, response
bodies or email content.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import settings

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs'
GMAIL_API = 'https://gmail.googleapis.com/gmail/v1/users/me'
GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
SCOPES = ('openid', 'email', GMAIL_SCOPE)
ISSUERS = ('https://accounts.google.com', 'accounts.google.com')
CLOCK_SKEW_SECONDS = 60


class GoogleError(Exception):
    """Base error. ``code`` is a safe category for logs and the UI."""

    code = 'google_error'


class InvalidGrant(GoogleError):
    """The refresh token was revoked or expired (Testing-mode tokens expire after 7 days)."""

    code = 'reauth_required'


class GoogleUnavailable(GoogleError):
    code = 'gmail_unavailable'


class GoogleRejected(GoogleError):
    code = 'gmail_rejected'


class GmailBadRequest(GoogleError):
    """Gmail rejected the request itself (e.g. an expired or invalid page token)."""

    code = 'gmail_bad_request'


class MessageGone(GoogleError):
    code = 'message_gone'


class IdTokenInvalid(GoogleError):
    code = 'invalid_identity'


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    scopes: list[str]
    id_token: str | None


@dataclass
class Identity:
    sub: str
    email: str


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')


def authorization_url(*, state: str, verifier: str, nonce: str, login_hint: str | None) -> str:
    params = {
        'client_id': settings.google_oauth_client_id,
        'redirect_uri': settings.google_oauth_redirect_uri,
        'response_type': 'code',
        'scope': ' '.join(SCOPES),
        'state': state,
        'nonce': nonce,
        'code_challenge': pkce_challenge(verifier),
        'code_challenge_method': 'S256',
        'access_type': 'offline',
        # Ask again so Google normally issues a refresh token; the callback still handles its absence.
        'prompt': 'consent',
        'include_granted_scopes': 'false',
    }
    if login_hint:
        params['login_hint'] = login_hint
    return f'{AUTH_URL}?{urlencode(params)}'


def _token_response(data: dict[str, Any]) -> TokenResponse:
    access = data.get('access_token')
    if not isinstance(access, str) or not access:
        raise GoogleRejected()
    refresh = data.get('refresh_token')
    return TokenResponse(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        scopes=str(data.get('scope') or '').split(),
        id_token=data.get('id_token') if isinstance(data.get('id_token'), str) else None,
    )


class GoogleApi:
    def __init__(self) -> None:
        self._jwks: jwt.PyJWKClient | None = None

    async def _post_form(self, url: str, form: dict[str, str]) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                return await client.post(url, data=form)
        except httpx.HTTPError:
            raise GoogleUnavailable() from None

    async def exchange_code(self, code: str, verifier: str) -> TokenResponse:
        response = await self._post_form(TOKEN_URL, {
            'code': code,
            'client_id': settings.google_oauth_client_id or '',
            'client_secret': settings.google_oauth_client_secret or '',
            'redirect_uri': settings.google_oauth_redirect_uri,
            'grant_type': 'authorization_code',
            'code_verifier': verifier,
        })
        if response.status_code >= 500:
            raise GoogleUnavailable()
        if response.status_code != 200:
            raise GoogleRejected()
        return _token_response(response.json())

    async def refresh(self, refresh_token: str) -> TokenResponse:
        response = await self._post_form(TOKEN_URL, {
            'client_id': settings.google_oauth_client_id or '',
            'client_secret': settings.google_oauth_client_secret or '',
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token',
        })
        if response.status_code >= 500:
            raise GoogleUnavailable()
        if response.status_code != 200:
            try:
                error = response.json().get('error')
            except ValueError:
                error = None
            if error in ('invalid_grant', 'unauthorized_client'):
                raise InvalidGrant()
            raise GoogleRejected()
        return _token_response(response.json())

    async def revoke(self, token: str) -> None:
        """Best effort. Only ever called for a token owned by the disconnecting user."""
        try:
            await self._post_form(REVOKE_URL, {'token': token})
        except GoogleError:
            pass

    def _verify_id_token_sync(self, token: str, nonce: str) -> Identity:
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(JWKS_URL, cache_keys=True)
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=['RS256'], audience=settings.google_oauth_client_id,
                options={'require': ['iss', 'aud', 'exp', 'sub', 'iat']},
                leeway=CLOCK_SKEW_SECONDS,  # tolerate small clock differences; signature, aud, iss still strict
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError):
            raise IdTokenInvalid() from None
        return check_claims(claims, nonce)

    async def verify_id_token(self, token: str, nonce: str) -> Identity:
        return await asyncio.to_thread(self._verify_id_token_sync, token, nonce)

    async def _gmail(self, access_token: str, path: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f'{GMAIL_API}/{path}', params=params,
                                            headers={'Authorization': f'Bearer {access_token}'})
        except httpx.HTTPError:
            raise GoogleUnavailable() from None
        if response.status_code == 404:
            raise MessageGone()
        if response.status_code in (401, 403):
            raise GoogleRejected()
        if response.status_code == 400:
            raise GmailBadRequest()
        if response.status_code != 200:
            raise GoogleUnavailable()
        return response.json()

    async def list_messages(self, access_token: str, query: str, page_token: str | None) -> dict[str, Any]:
        params = [('q', query), ('maxResults', '100'), ('includeSpamTrash', 'false')]
        if page_token:
            params.append(('pageToken', page_token))
        return await self._gmail(access_token, 'messages', params)

    async def get_metadata(self, access_token: str, message_id: str) -> dict[str, Any]:
        params = [('format', 'metadata')] + [('metadataHeaders', h) for h in ('From', 'Subject', 'List-Unsubscribe')]
        return await self._gmail(access_token, f'messages/{message_id}', params)

    async def get_full(self, access_token: str, message_id: str) -> dict[str, Any]:
        # format=full returns body parts inline; attachment bodies are only referenced by id and never fetched.
        return await self._gmail(access_token, f'messages/{message_id}', [('format', 'full')])


def check_claims(claims: dict[str, Any], nonce: str) -> Identity:
    if claims.get('iss') not in ISSUERS or claims.get('aud') != settings.google_oauth_client_id:
        raise IdTokenInvalid()
    if not nonce or claims.get('nonce') != nonce:
        raise IdTokenInvalid()
    sub, email = claims.get('sub'), claims.get('email')
    if not isinstance(sub, str) or not sub or not isinstance(email, str) or '@' not in email:
        raise IdTokenInvalid()
    return Identity(sub=sub, email=email.lower())


google = GoogleApi()


def api() -> GoogleApi:
    """The active client (tests substitute ``google`` with a fake)."""
    return google
