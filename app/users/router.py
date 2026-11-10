from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError

from app.auth.dependencies import get_current_user
from app.auth.inputs import AuthPayloadRoute
from app.auth.models import RefreshToken
from app.auth.sessions import Token, issue_tokens
from app.auth.security import hash_password, verify_password
from app.users.models import User
from app.users.schemas import PasswordChange, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])
account_router = APIRouter(route_class=AuthPayloadRoute)

CurrentUser = Annotated[User, Depends(get_current_user)]


async def check_current_password(user: User, password: str | None) -> None:
    # 403, not 401: the frontend treats 401 as "access token expired, refresh it".
    if not password or not await verify_password(password, user.hashed_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")


@router.get("/me", response_model=UserRead)
async def read_me(user: CurrentUser) -> User:
    return user


@account_router.patch("/me", response_model=UserRead)
async def update_me(data: UserUpdate, user: CurrentUser) -> User:
    if data.email is not None and data.email.lower() != user.email:
        await check_current_password(user, data.current_password)
        user.email = data.email.lower()
    if data.full_name is not None:
        user.full_name = data.full_name
    try:
        await user.save()
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    return user


@account_router.post("/me/password")
async def change_password(data: PasswordChange, user: CurrentUser) -> Token:
    """Change the password, log out all other sessions and return new tokens for this one."""
    await check_current_password(user, data.current_password)
    user.hashed_password = await hash_password(data.new_password)
    await user.save()
    await RefreshToken.find(RefreshToken.user_id == user.id).delete()
    return await issue_tokens(user.id)


router.include_router(account_router)
