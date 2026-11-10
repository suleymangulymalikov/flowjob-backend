"""Shared authentication throttles enforced by atomic MongoDB updates."""

import hmac
import ipaddress
from datetime import UTC, datetime
from time import time
from typing import Literal

from fastapi import HTTPException, Request
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.auth.models import AuthRateBucket
from app.config import settings

WINDOW_SECONDS = 15 * 60
LOGIN_PER_NETWORK = 40
LOGIN_PER_ACCOUNT = 10
REGISTER_PER_NETWORK = 12


def _client_network(request: Request) -> str:
    host = request.client.host if request.client else 'unknown'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return 'unknown'
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network((ip, 64), strict=False))
    return str(ip)


async def _charge(scope: str, subject: str, limit: int, now: int) -> None:
    window = now // WINDOW_SECONDS
    end = (window + 1) * WINDOW_SECONDS
    # HMAC avoids storing the IP address or email address in quota documents.
    digest = hmac.digest(settings.jwt_secret.encode(), f'{scope}:{window}:{subject}'.encode(), 'sha256').hex()
    collection = AuthRateBucket.get_pymongo_collection()
    query = {'_id': digest}
    update = [{'$set': {
        'attempts': {'$min': [limit, {'$add': [{'$ifNull': ['$attempts', 0]}, 1]}]},
        'expires_at': datetime.fromtimestamp(end, UTC),
    }}]
    try:
        try:
            previous = await collection.find_one_and_update(
                query, update, upsert=True, return_document=ReturnDocument.BEFORE,
            )
        except DuplicateKeyError:
            # A simultaneous first request may have created the same bucket.
            previous = await collection.find_one_and_update(query, update, return_document=ReturnDocument.BEFORE)
            if previous is None:
                raise HTTPException(503, 'Authentication is temporarily unavailable. Please try again.')
    except PyMongoError:
        # Fail closed before starting a password hash when the limit cannot be checked.
        raise HTTPException(503, 'Authentication is temporarily unavailable. Please try again.') from None
    if previous is not None and previous['attempts'] >= limit:
        raise HTTPException(
            429, 'Too many authentication attempts. Please try again later.',
            headers={'Retry-After': str(end - now)},
        )


async def throttle(request: Request, action: Literal['login', 'register'], email: str = '') -> None:
    now = int(time())
    network = _client_network(request)
    if action == 'register':
        await _charge('register-network', network, REGISTER_PER_NETWORK, now)
    else:
        await _charge('login-network', network, LOGIN_PER_NETWORK, now)
        await _charge('login-account', email, LOGIN_PER_ACCOUNT, now)
