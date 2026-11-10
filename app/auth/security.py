import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from anyio import CapacityLimiter, to_thread
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.config import settings

ALGORITHM = "HS256"

password_hash = PasswordHash.recommended()
_password_slots = CapacityLimiter(4)
# Public synthetic hash with the same Argon2 settings as normal accounts.
# It is only used to equalize verification work for unknown email addresses.
_DUMMY_HASH = '$argon2id$v=19$m=65536,t=3,p=4$fo78mlGBkteF8ttWORPi4w$waP0DesTZRWC5VGZ3VT+sHUEBc684EnTy9QDJh1bXYU'


async def hash_password(password: str) -> str:
    return await to_thread.run_sync(password_hash.hash, password, limiter=_password_slots)


def _verify(password: str, hashed_password: str | None) -> bool:
    try:
        matches = password_hash.verify(password, hashed_password or _DUMMY_HASH)
        return bool(hashed_password) and matches
    except UnknownHashError:
        return False


async def verify_password(password: str, hashed_password: str | None) -> bool:
    return await to_thread.run_sync(_verify, password, hashed_password, limiter=_password_slots)


def create_access_token(user_id: str) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """Return the user id from a valid token, or None if invalid or expired."""
    if len(token) > 4096:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM], options={'require': ['sub', 'exp']})
    except jwt.InvalidTokenError:
        return None
    return payload.get("sub")


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    # Tokens are long and random, so a fast hash is enough (unlike passwords).
    return hashlib.sha256(token.encode()).hexdigest()
