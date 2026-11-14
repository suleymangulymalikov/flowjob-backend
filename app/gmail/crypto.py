"""AES-256-GCM encryption for stored Google credentials.

Format: ``v1.<key id>.<base64url(nonce || ciphertext+tag)>``. The key id is a short digest of the key, so a
rotated-out key (listed in GMAIL_TOKEN_KEYS_OLD) can still decrypt values written before the rotation while new
values always use the current key. The associated data binds a ciphertext to its owner (e.g. FlowJob user id and
Google ``sub``): a value copied into another user's record fails authentication instead of decrypting.

Rotation path: set the new key as GMAIL_TOKEN_KEY, move the old key into GMAIL_TOKEN_KEYS_OLD, restart. Each
connection is re-encrypted with the new key the next time its token is used (``needs_rotation``). Once every
connection has synchronized (or reconnected), remove the old key.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

VERSION = 'v1'


class TokenCryptoError(Exception):
    """Decryption or configuration failure. Never carries key or plaintext material."""


def _decode_key(raw: str) -> bytes:
    value = raw.strip()
    padded = value + '=' * (-len(value) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        if len(key) == 32:
            return key
    raise TokenCryptoError('GMAIL_TOKEN_KEY must be a base64-encoded 32-byte key.')


def _key_id(key: bytes) -> str:
    return hashlib.sha256(b'flowjob-gmail-key:' + key).hexdigest()[:10]


def _keys() -> tuple[bytes, dict[str, bytes]]:
    if not settings.gmail_token_key:
        raise TokenCryptoError('Gmail token encryption is not configured.')
    current = _decode_key(settings.gmail_token_key)
    ring = {_key_id(current): current}
    for raw in filter(None, (part.strip() for part in settings.gmail_token_keys_old.split(','))):
        old = _decode_key(raw)
        ring.setdefault(_key_id(old), old)
    return current, ring


def encrypt(plaintext: str, aad: str) -> str:
    current, _ = _keys()
    nonce = os.urandom(12)
    sealed = AESGCM(current).encrypt(nonce, plaintext.encode('utf-8'), aad.encode('utf-8'))
    body = base64.urlsafe_b64encode(nonce + sealed).decode('ascii').rstrip('=')
    return f'{VERSION}.{_key_id(current)}.{body}'


def decrypt(token: str, aad: str) -> str:
    _, ring = _keys()
    try:
        version, kid, body = token.split('.', 2)
    except (AttributeError, ValueError):
        raise TokenCryptoError('Malformed encrypted value.') from None
    if version != VERSION or kid not in ring:
        raise TokenCryptoError('Encrypted value uses an unknown key.')
    try:
        raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))
        plain = AESGCM(ring[kid]).decrypt(raw[:12], raw[12:], aad.encode('utf-8'))
    except (binascii.Error, ValueError, InvalidTag):
        raise TokenCryptoError('Encrypted value failed authentication.') from None
    return plain.decode('utf-8')


def needs_rotation(token: str) -> bool:
    current, _ = _keys()
    parts = token.split('.', 2)
    return len(parts) != 3 or parts[1] != _key_id(current)


def connection_aad(user_id: object, google_sub: str) -> str:
    return f'gmail-refresh:{user_id}:{google_sub}'
