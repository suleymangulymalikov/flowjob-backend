"""Token issuance, atomic one-time refresh consumption, and revocation."""

from datetime import UTC, datetime, timedelta

from beanie import PydanticObjectId
from pydantic import BaseModel

from app.auth.models import RefreshToken
from app.auth.security import create_access_token, generate_refresh_token, hash_refresh_token, verify_password
from app.config import settings
from app.users.models import User


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = 'bearer'


async def authenticate(email: str, password: str) -> User | None:
    user = await User.find_one(User.email == email)
    valid = await verify_password(password, user.hashed_password if user else None)
    return user if valid else None


async def issue_tokens(user_id: PydanticObjectId) -> Token:
    refresh_token = generate_refresh_token()
    await RefreshToken(
        token_hash=hash_refresh_token(refresh_token),
        user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    ).insert()
    return Token(access_token=create_access_token(str(user_id)), refresh_token=refresh_token)


def _token_hash(token: str) -> str | None:
    return hash_refresh_token(token) if 0 < len(token) <= 256 and token.isascii() else None


async def consume_refresh_token(token: str) -> User | None:
    digest = _token_hash(token)
    if not digest:
        return None
    # Exactly one request can remove and exchange a given unexpired token.
    stored = await RefreshToken.get_pymongo_collection().find_one_and_delete({
        'token_hash': digest, 'expires_at': {'$gt': datetime.now(UTC)},
    })
    return await User.get(stored['user_id']) if stored else None


async def revoke_refresh_token(token: str) -> None:
    if digest := _token_hash(token):
        await RefreshToken.get_pymongo_collection().delete_one({'token_hash': digest})
