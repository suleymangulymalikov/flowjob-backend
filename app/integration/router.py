"""Versioned browser-facing API. Legacy FastAPI endpoints remain available.

The frontend's unfinished matching, tracking and Gmail APIs are intentionally
NOT stubbed: they return 404 until implemented, rather than fake success.
"""
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, Field
from pymongo.errors import DuplicateKeyError

from app.auth.dependencies import get_current_user
from app.cv.render import render_saved_cv_pdf
from app.auth.models import RefreshToken
from app.auth.router import issue_tokens
from app.auth.security import hash_password, hash_refresh_token, verify_password
from app.config import settings
from app.integration.models import SavedCv
from app.profiles.models import Profile
from app.profiles.schemas import Education, Experience, Personal, Project
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['frontend'])
CurrentUser = Annotated[User, Depends(get_current_user)]
COOKIE = 'flowjob_refresh'


def present_user(user: User) -> dict[str, str]:
    return {'id': str(user.id), 'name': user.full_name, 'email': user.email, 'createdAt': user.created_at.isoformat()}


def attach_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        COOKIE, refresh_token, httponly=True, secure=settings.cookie_secure,
        samesite='lax', path='/api/v1/auth',
        max_age=settings.refresh_token_expire_days * 86400,
    )


async def session(user: User, response: Response) -> dict[str, Any]:
    tokens = await issue_tokens(user.id)
    attach_cookie(response, tokens.refresh_token)
    return {'accessToken': tokens.access_token, 'user': present_user(user)}


class LoginData(BaseModel):
    email: EmailStr
    password: str


class RegisterData(LoginData):
    name: str = Field(min_length=1)


@router.post('/auth/register', status_code=201)
async def register(data: RegisterData, response: Response):
    user = User(email=data.email.lower(), full_name=data.name, hashed_password=hash_password(data.password))
    if len(data.password) < 8:
        raise HTTPException(422, 'Password must contain at least 8 characters')
    try:
        await user.insert()
    except DuplicateKeyError:
        raise HTTPException(409, 'Email already registered')
    return await session(user, response)


@router.post('/auth/login')
async def login(data: LoginData, response: Response):
    user = await User.find_one(User.email == data.email.lower())
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(401, 'Incorrect email or password')
    return await session(user, response)


@router.post('/auth/refresh')
async def refresh(response: Response, flowjob_refresh: Annotated[str | None, Cookie()] = None):
    if not flowjob_refresh:
        raise HTTPException(401, 'Missing session')
    stored = await RefreshToken.find_one(
        RefreshToken.token_hash == hash_refresh_token(flowjob_refresh),
        RefreshToken.expires_at > datetime.now(UTC),
    )
    if stored is None or (await stored.delete()).deleted_count == 0:
        response.delete_cookie(COOKIE, path='/api/v1/auth')
        raise HTTPException(401, 'Invalid session')
    user = await User.get(stored.user_id)
    if user is None:
        raise HTTPException(401, 'User no longer exists')
    return await session(user, response)


@router.post('/auth/logout', status_code=204)
async def logout(response: Response, flowjob_refresh: Annotated[str | None, Cookie()] = None):
    if flowjob_refresh:
        stored = await RefreshToken.find_one(RefreshToken.token_hash == hash_refresh_token(flowjob_refresh))
        if stored:
            await stored.delete()
    response.delete_cookie(COOKIE, path='/api/v1/auth')


def blank_profile(user: User) -> dict[str, Any]:
    return {'fullName': user.full_name, 'email': user.email, 'phone': '', 'location': '',
            'links': [], 'summary': '', 'experience': [], 'education': [],
            'projects': [], 'skills': [], 'languages': ''}


def native_to_ui(profile: Profile, user: User) -> dict[str, Any]:
    p = blank_profile(user)
    p.update({
        'phone': profile.personal.phone or '',
        'location': profile.personal.location or '',
        'summary': profile.personal.summary or '',
        'links': [v for v in [profile.personal.linkedin, profile.personal.github, profile.personal.website] if v],
        'experience': [{'id': str(i), 'title': x.position, 'company': x.company, 'location': x.location or '',
                        'start': x.start, 'end': x.end or '', 'bullets': x.bullets} for i, x in enumerate(profile.experience)],
        'education': [{'id': str(i), 'school': x.institution, 'degree': x.degree, 'start': x.start,
                       'end': x.end or '', 'note': x.description or ''} for i, x in enumerate(profile.education)],
        'projects': [{'id': str(i), 'name': x.name, 'tech': ', '.join(x.technologies),
                      'description': x.description or ''} for i, x in enumerate(profile.projects)],
        'skills': profile.skills,
        'languages': ', '.join(f'{x.name} {x.level}' for x in profile.languages),
    })
    return p


@router.get('/profile')
async def read_profile(user: CurrentUser):
    profile = await Profile.find_one(Profile.user_id == user.id)
    if not profile:
        return blank_profile(user)
    return profile.frontend_data or native_to_ui(profile, user)


class UIProfile(BaseModel):
    fullName: str = ''
    email: str = ''
    phone: str = ''
    location: str = ''
    links: list[str] = Field(default_factory=list)
    summary: str = ''
    experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: str = ''


def year_month(s: Any) -> str | None:
    # The UI permits freeform dates; only valid YYYY-MM is mirrored into the native PDF model.
    import re
    return s if isinstance(s, str) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', s) else None


@router.put('/profile')
async def save_profile(data: UIProfile, user: CurrentUser):
    profile = await Profile.find_one(Profile.user_id == user.id) or Profile(user_id=user.id)
    profile.frontend_data = data.model_dump()
    links = data.links + ['', '', '']
    profile.personal = Personal(phone=data.phone, location=data.location, summary=data.summary,
                                linkedin=links[0] or None, github=links[1] or None,
                                website=links[2] or None)
    profile.skills = data.skills
    profile.experience = [Experience(company=str(x.get('company') or ''), position=str(x.get('title') or ''),
                                     location=x.get('location') or None, start=year_month(x.get('start')),
                                     end=year_month(x.get('end')), bullets=x.get('bullets') or [])
                          for x in data.experience if year_month(x.get('start'))]
    profile.education = [Education(institution=str(x.get('school') or ''), degree=str(x.get('degree') or ''),
                                   start=year_month(x.get('start')), end=year_month(x.get('end')),
                                   description=x.get('note') or None)
                         for x in data.education if year_month(x.get('start'))]
    profile.projects = [Project(name=str(x.get('name') or ''), description=x.get('description') or None,
                                technologies=[s.strip() for s in str(x.get('tech') or '').split(',') if s.strip()])
                        for x in data.projects]
    # Freeform language text is preserved in frontend_data; native levels require structured input.
    await profile.save()
    return profile.frontend_data


def present_cv(cv: SavedCv) -> dict[str, Any]:
    data: dict[str, Any] = {'id': str(cv.id), 'name': cv.name, 'kind': cv.kind,
        'createdAt': cv.created_at.isoformat(), 'updatedAt': cv.updated_at.isoformat(), 'content': cv.content}
    if cv.job_title: data['jobTitle'] = cv.job_title
    if cv.company: data['company'] = cv.company
    if cv.match_id: data['matchId'] = cv.match_id
    if cv.source_cv_id: data['sourceCvId'] = cv.source_cv_id
    return data


class CVInput(BaseModel):
    name: str | None = None
    kind: Literal['original', 'tailored'] | None = None
    jobTitle: str | None = None
    company: str | None = None
    matchId: str | None = None
    sourceCvId: str | None = None
    content: dict[str, Any] | None = None


async def owned_cv(cv_id: str, user: User) -> SavedCv:
    try:
        oid = PydanticObjectId(cv_id)
    except (ValueError, TypeError, InvalidId):
        raise HTTPException(404, 'CV not found')
    cv = await SavedCv.find_one(SavedCv.id == oid, SavedCv.user_id == user.id)
    if not cv:
        raise HTTPException(404, 'CV not found')
    return cv


@router.get('/cvs')
async def cvs(user: CurrentUser):
    return [present_cv(c) for c in await SavedCv.find(SavedCv.user_id == user.id).to_list()]


@router.post('/cvs', status_code=201)
async def create_cv(data: CVInput, user: CurrentUser):
    content = data.content or (await read_profile(user))
    cv = SavedCv(user_id=user.id, name=data.name or 'Original CV', kind=data.kind or 'original',
                 job_title=data.jobTitle, company=data.company, match_id=data.matchId,
                 source_cv_id=data.sourceCvId, content=content)
    await cv.insert()
    return present_cv(cv)


@router.get('/cvs/{cv_id}')
async def get_cv(cv_id: str, user: CurrentUser):
    return present_cv(await owned_cv(cv_id, user))


@router.put('/cvs/{cv_id}')
async def update_cv(cv_id: str, data: CVInput, user: CurrentUser):
    cv = await owned_cv(cv_id, user)
    for attr, value in [('name', data.name), ('kind', data.kind), ('job_title', data.jobTitle),
                        ('company', data.company), ('match_id', data.matchId), ('content', data.content)]:
        if value is not None:
            setattr(cv, attr, value)
    cv.updated_at = datetime.now(UTC)
    await cv.save()
    return present_cv(cv)


@router.delete('/cvs/{cv_id}', status_code=204)
async def delete_cv(cv_id: str, user: CurrentUser):
    await (await owned_cv(cv_id, user)).delete()


@router.get('/cvs/{cv_id}/pdf', response_class=Response,
            responses={200: {'content': {'application/pdf': {}}}})
async def saved_cv_pdf(cv_id: str, user: CurrentUser) -> Response:
    """Render a saved CV's own content with the canonical Harvard template.

    Ownership is enforced by owned_cv (404 for anyone else's CV). The same document
    backs both the browser preview and the download.
    """
    cv = await owned_cv(cv_id, user)
    content = cv.content or {}
    name = str(content.get('fullName') or user.full_name)
    email = str(content.get('email') or user.email)
    # PDF rendering is CPU-bound, so run it off the event loop.
    pdf = await run_in_threadpool(render_saved_cv_pdf, name, email, content)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', cv.name).strip('_') or 'CV'
    return Response(
        pdf,
        media_type='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{safe}.pdf"'},
    )


class PreviewInput(BaseModel):
    content: dict[str, Any] = Field(default_factory=dict)


@router.post('/cvs/preview/pdf', response_class=Response,
             responses={200: {'content': {'application/pdf': {}}}})
async def preview_cv_pdf(data: PreviewInput, user: CurrentUser) -> Response:
    """Render arbitrary (unsaved) CV content for the live builder preview.

    Stateless: uses the same Harvard renderer as saved previews and downloads, and
    never reads or writes any MongoDB record (only the authenticated user is needed).
    """
    content = data.content or {}
    name = str(content.get('fullName') or user.full_name)
    email = str(content.get('email') or user.email)
    pdf = await run_in_threadpool(render_saved_cv_pdf, name, email, content)
    return Response(pdf, media_type='application/pdf',
                    headers={'Content-Disposition': 'inline; filename="preview.pdf"'})


class MeUpdate(BaseModel):
    name: str | None = Field(None, min_length=1)
    email: EmailStr | None = None
    currentPassword: str | None = None


@router.patch('/me')
async def update_me(data: MeUpdate, user: CurrentUser):
    if data.email and data.email.lower() != user.email:
        if not data.currentPassword or not verify_password(data.currentPassword, user.hashed_password):
            raise HTTPException(403, 'Current password required to change email')
        user.email = data.email.lower()
    if data.name is not None:
        user.full_name = data.name
    try:
        await user.save()
    except DuplicateKeyError:
        raise HTTPException(409, 'Email already registered')
    profile = await Profile.find_one(Profile.user_id == user.id)
    if profile and profile.frontend_data is not None:
        profile.frontend_data = {**profile.frontend_data, 'fullName': user.full_name, 'email': user.email}
        await profile.save()
    return present_user(user)


class PasswordInput(BaseModel):
    current: str
    next: str = Field(min_length=8)


@router.post('/me/password', status_code=204)
async def change_password(data: PasswordInput, user: CurrentUser, response: Response):
    if not verify_password(data.current, user.hashed_password):
        raise HTTPException(403, 'Incorrect password')
    user.hashed_password = hash_password(data.next)
    await user.save()
    # Revoke other sessions while keeping this browser signed in.
    await RefreshToken.find(RefreshToken.user_id == user.id).delete()
    await session(user, response)

