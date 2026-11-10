from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, TypeAdapter, ValidationError
from pymongo.errors import DuplicateKeyError

from app.auth.inputs import AuthPayloadRoute, LoginIdentifier, LoginPassword, RefreshInput
from app.auth.limits import throttle
from app.auth.security import hash_password
from app.auth.sessions import Token, authenticate, consume_refresh_token, issue_tokens, revoke_refresh_token
from app.users.models import User
from app.users.schemas import UserCreate, UserRead

router = APIRouter(prefix='/auth', tags=['auth'], route_class=AuthPayloadRoute)
_login_identifier = TypeAdapter(LoginIdentifier)
_login_password = TypeAdapter(LoginPassword)


class RefreshRequest(BaseModel):
    refresh_token: RefreshInput


@router.post('/register', response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate, request: Request) -> User:
    email = data.email.lower()
    await throttle(request, 'register')
    user = User(email=email, hashed_password=await hash_password(data.password), full_name=data.full_name)
    try:
        await user.insert()
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, 'Email already registered') from None
    return user


@router.post('/login')
async def login(request: Request, form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> Token:
    # Keep the OAuth2 form contract while applying the same input bounds as JSON login.
    try:
        email = _login_identifier.validate_python(form.username.strip().lower())
    except ValidationError:
        raise HTTPException(422, 'Enter a valid email address.') from None
    try:
        password = _login_password.validate_python(form.password)
    except ValidationError:
        raise HTTPException(422, 'Enter a password of at most 1024 characters using valid Unicode.') from None
    await throttle(request, 'login', email)
    user = await authenticate(email, password)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, 'Incorrect email or password',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    return await issue_tokens(user.id)


@router.post('/refresh')
async def refresh(data: RefreshRequest) -> Token:
    user = await consume_refresh_token(data.refresh_token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid or expired refresh token')
    return await issue_tokens(user.id)


@router.post('/logout', status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest) -> None:
    await revoke_refresh_token(data.refresh_token)
