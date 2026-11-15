"""Server-side Google authorization-code flow with PKCE, single-use state and a browser-binding cookie.

Start (authenticated XHR): create a hashed, 10-minute, single-use state bound to the FlowJob user, an encrypted
PKCE verifier, an OIDC nonce, and an HttpOnly cookie nonce for this browser. Callback (top-level redirect from
Google, no bearer token): consume the state atomically, require the cookie to match, exchange the code, verify the
ID token and the granted Gmail scope, then apply the ownership rules:

* a Google account (``sub``) belongs to at most one FlowJob user: a conflict is refused WITHOUT revoking the new
  token, because revocation would also cut off the other user's grant;
* reconnect must use the same Google account; switching accounts requires an explicit disconnect first.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.gmail import crypto
from app.gmail.connection import now
from app.gmail.google import GMAIL_SCOPE, GoogleError, InvalidGrant, authorization_url, api
from app.gmail.models import GmailConnection, GmailOAuthState
from app.users.models import User

STATE_TTL = timedelta(minutes=10)
COOKIE = 'flowjob_gmail_oauth'
COOKIE_PATH = '/api/v1/gmail/oauth'


CALLBACK_PATH = '/api/v1/gmail/oauth/callback'


class RedactOAuthCallback(logging.Filter):
    """Access-log filter: the OAuth callback's query string (authorization code, state) never reaches a log.

    Uvicorn's access records carry the request target as the third argument; only that path is rewritten.
    Reverse proxies in front of the backend must be configured likewise (see README, "Production").
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                and args[2].startswith(CALLBACK_PATH + '?'):
            record.args = (*args[:2], CALLBACK_PATH + '?[redacted]', *args[3:])
        return True


def install_log_redaction() -> None:
    access = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, RedactOAuthCallback) for f in access.filters):
        access.addFilter(RedactOAuthCallback())


class OAuthFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def valid_timezone(value: str | None) -> str:
    """The browser's IANA zone, or UTC when missing/invalid."""
    if not value or len(value) > 64:
        return 'UTC'
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return 'UTC'
    return value


@dataclass
class StartResult:
    url: str
    cookie_value: str


async def start(user: User, mode: str, timezone: str | None) -> StartResult:
    existing = await GmailConnection.find_one(GmailConnection.user_id == user.id, ignore_cache=True)
    if existing and existing.status == 'disconnecting':
        raise OAuthFailure('disconnect_in_progress')
    if mode == 'connect' and existing:
        raise OAuthFailure('already_connected')
    if mode == 'reconnect' and not existing:
        raise OAuthFailure('not_connected')

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43-128
    browser_nonce = secrets.token_urlsafe(32)
    oidc_nonce = secrets.token_urlsafe(24)
    state_hash = _digest(state)
    # Only the latest flow per user stays valid (the browser cookie is overwritten anyway); this also bounds
    # how many pending states one account can create.
    await GmailOAuthState.get_pymongo_collection().delete_many({'user_id': user.id})
    await GmailOAuthState(
        state_hash=state_hash, user_id=user.id, mode=mode, nonce_hash=_digest(browser_nonce),
        enc_verifier=crypto.encrypt(verifier, f'gmail-oauth:{state_hash}'), oidc_nonce=oidc_nonce,
        timezone=valid_timezone(timezone), expires_at=now() + STATE_TTL,
    ).insert()
    url = authorization_url(state=state, verifier=verifier, nonce=oidc_nonce,
                            login_hint=existing.email if existing else None)
    return StartResult(url=url, cookie_value=browser_nonce)


@dataclass
class CallbackResult:
    connection: GmailConnection
    outcome: str  # connected | reconnected


async def _consume_state(state: str | None) -> GmailOAuthState | None:
    if not state or len(state) > 256:
        return None
    doc = await GmailOAuthState.get_pymongo_collection().find_one_and_delete(
        {'state_hash': _digest(state), 'expires_at': {'$gt': now()}})
    return GmailOAuthState.model_validate(doc) if doc else None


async def complete(*, code: str | None, state: str | None, error: str | None,
                   cookie: str | None) -> CallbackResult:
    record = await _consume_state(state)  # single use: consumed even when the user denied access
    if record is None:
        raise OAuthFailure('invalid_state')
    # Browser binding: the flow must finish in the browser that started it.
    if not cookie or not hmac.compare_digest(_digest(cookie), record.nonce_hash):
        raise OAuthFailure('invalid_state')
    if error or not code:
        raise OAuthFailure('denied')
    user = await User.get(record.user_id)
    if user is None:
        raise OAuthFailure('invalid_state')

    verifier = crypto.decrypt(record.enc_verifier, f'gmail-oauth:{record.state_hash}')
    try:
        tokens = await api().exchange_code(code, verifier)
        if not tokens.id_token:
            raise OAuthFailure('invalid_identity')
        identity = await api().verify_id_token(tokens.id_token, record.oidc_nonce)
    except GoogleError as failure:
        raise OAuthFailure('invalid_identity' if failure.code == 'invalid_identity' else 'exchange_failed') from None
    if GMAIL_SCOPE not in tokens.scopes:
        raise OAuthFailure('scope_missing')

    owner = await GmailConnection.find_one(GmailConnection.google_sub == identity.sub, ignore_cache=True)
    if owner and owner.user_id != user.id:
        raise OAuthFailure('account_in_use')  # the new token is discarded, never revoked
    mine = await GmailConnection.find_one(GmailConnection.user_id == user.id, ignore_cache=True)
    if mine and mine.status == 'disconnecting':
        raise OAuthFailure('disconnect_in_progress')
    if mine and mine.google_sub != identity.sub:
        raise OAuthFailure('different_account' if record.mode == 'reconnect' else 'already_connected')
    if record.mode == 'connect' and mine and mine.status == 'active':
        raise OAuthFailure('already_connected')

    aad = crypto.connection_aad(user.id, identity.sub)
    refresh = tokens.refresh_token
    if refresh is None:
        # Google may omit the refresh token for an existing grant. Keep the stored one only if it still works.
        if not mine:
            raise OAuthFailure('no_refresh_token')
        try:
            refresh = crypto.decrypt(mine.enc_refresh_token, aad)
            await api().refresh(refresh)
        except (InvalidGrant, GoogleError, crypto.TokenCryptoError):
            raise OAuthFailure('no_refresh_token') from None
    enc = crypto.encrypt(refresh, aad)

    if mine:
        # Reconnect keeps all progress, pending reviews and suppressions.
        attention = mine.attention if mine.attention != 'reauth_required' else None
        result = await GmailConnection.get_pymongo_collection().find_one_and_update(
            {'_id': mine.id, 'google_sub': identity.sub, 'status': {'$ne': 'disconnecting'}},
            {'$set': {'status': 'active', 'enc_refresh_token': enc, 'scopes': tokens.scopes,
                      'email': identity.email, 'timezone': record.timezone, 'attention': attention,
                      'updated_at': now()}},
            return_document=True,
        )
        if result is None:
            raise OAuthFailure('disconnect_in_progress')
        return CallbackResult(GmailConnection.model_validate(result), 'reconnected')

    conn = GmailConnection(
        id=PydanticObjectId(), user_id=user.id, google_sub=identity.sub, email=identity.email,
        enc_refresh_token=enc, scopes=tokens.scopes, timezone=record.timezone,
    )
    try:
        await conn.insert()
    except DuplicateKeyError:
        # A concurrent callback claimed this Google account or this user first.
        raise OAuthFailure('account_in_use') from None
    return CallbackResult(conn, 'connected')
