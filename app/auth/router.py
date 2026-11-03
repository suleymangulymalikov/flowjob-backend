from datetime import UTC, datetime, timedelta
from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.auth.models import RefreshToken
from app.auth.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.config import settings
from app.users.models import User
from app.users.schemas import UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


async def issue_tokens(user_id: PydanticObjectId) -> Token:
    refresh_token = generate_refresh_token()
    await RefreshToken(
        token_hash=hash_refresh_token(refresh_token),
        user_id=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    ).insert()
    return Token(access_token=create_access_token(str(user_id)), refresh_token=refresh_token)


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate) -> User:
    user = User(
        email=data.email.lower(),
        hashed_password=hash_password(data.password),
        full_name=data.full_name,
    )
    try:
        await user.insert()
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    return user


@router.post("/login")
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> Token:
    # OAuth2 form calls the field "username"; we use it for the email.
    user = await User.find_one(User.email == form.username.lower())
    if user is None or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await issue_tokens(user.id)


@router.post("/refresh")
async def refresh(data: RefreshRequest) -> Token:
    """Exchange a refresh token for a new token pair. The old refresh token stops working."""
    stored = await RefreshToken.find_one(
        RefreshToken.token_hash == hash_refresh_token(data.refresh_token),
        RefreshToken.expires_at > datetime.now(UTC),
    )
    # deleted_count is 0 if a concurrent request already used this token.
    if stored is None or (await stored.delete()).deleted_count == 0:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    return await issue_tokens(stored.user_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest) -> None:
    await RefreshToken.find_one(
        RefreshToken.token_hash == hash_refresh_token(data.refresh_token)
    ).delete()
