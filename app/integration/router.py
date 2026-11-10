"""Versioned browser-facing API. Legacy FastAPI endpoints remain available.

The frontend's unfinished matching, tracking and Gmail APIs are intentionally
NOT stubbed: they return 404 until implemented, rather than fake success.
"""
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, ValidationError
from pymongo.errors import DuplicateKeyError

from app.auth.dependencies import get_current_user
from app.auth.inputs import AccountName, AuthPayloadRoute, ExistingPassword, LoginPassword, NewPassword
from app.auth.limits import throttle
from app.auth.models import RefreshToken
from app.auth.security import hash_password, verify_password
from app.auth.sessions import authenticate, consume_refresh_token, issue_tokens, revoke_refresh_token
from app.config import settings
from app.cv.content import (
    CvContent, CvCreateInput, CvPreviewInput, CvWriteInput,
    has_new_unsafe_links, read_cv_content, unsafe_contact_link,
)
from app.cv.http import CvPayloadRoute, checked_saved_content, render_pdf
from app.cv.render import render_saved_cv_pdf
from app.cv.revision import content_revision
from app.integration.models import SavedCv
from app.profiles.models import Profile
from app.profiles.schemas import Education, Experience, Personal, Project
from app.tailor.drafts import require_current_draft
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['frontend'])
cv_payload_router = APIRouter(route_class=CvPayloadRoute)
auth_payload_router = APIRouter(route_class=AuthPayloadRoute)
CurrentUser = Annotated[User, Depends(get_current_user)]
COOKIE = 'flowjob_refresh'


def present_user(user: User) -> dict[str, str]:
    return {'id': str(user.id), 'name': user.full_name, 'email': user.email, 'createdAt': user.created_at.isoformat()}


def secure_cookie(request: Request) -> bool:
    return settings.cookie_secure or request.url.scheme == 'https'


async def session(user: User, request: Request, response: Response) -> dict[str, Any]:
    tokens = await issue_tokens(user.id)
    response.set_cookie(
        COOKIE, tokens.refresh_token, httponly=True, secure=secure_cookie(request),
        samesite='lax', path='/api/v1/auth',
        max_age=settings.refresh_token_expire_days * 86400,
    )
    return {'accessToken': tokens.access_token, 'user': present_user(user)}


class LoginData(BaseModel):
    email: EmailStr
    password: LoginPassword


class RegisterData(BaseModel):
    email: EmailStr
    password: NewPassword
    name: AccountName


@auth_payload_router.post('/auth/register', status_code=201)
async def register(data: RegisterData, request: Request, response: Response):
    await throttle(request, 'register')
    user = User(email=data.email.lower(), full_name=data.name, hashed_password=await hash_password(data.password))
    try:
        await user.insert()
    except DuplicateKeyError:
        raise HTTPException(409, 'Email already registered') from None
    return await session(user, request, response)


@auth_payload_router.post('/auth/login')
async def login(data: LoginData, request: Request, response: Response):
    email = data.email.lower()
    await throttle(request, 'login', email)
    user = await authenticate(email, data.password)
    if user is None:
        raise HTTPException(401, 'Incorrect email or password')
    return await session(user, request, response)


@auth_payload_router.post('/auth/refresh')
async def refresh(request: Request, response: Response, flowjob_refresh: Annotated[str | None, Cookie()] = None):
    if not flowjob_refresh:
        raise HTTPException(401, 'Missing session')
    user = await consume_refresh_token(flowjob_refresh)
    if user is None:
        # A concurrent winning refresh may already have replaced the cookie.
        raise HTTPException(401, 'Invalid session')
    return await session(user, request, response)


@auth_payload_router.post('/auth/logout', status_code=204)
async def logout(request: Request, response: Response, flowjob_refresh: Annotated[str | None, Cookie()] = None):
    if flowjob_refresh:
        await revoke_refresh_token(flowjob_refresh)
    response.delete_cookie(COOKIE, path='/api/v1/auth', httponly=True, secure=secure_cookie(request), samesite='lax')


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
        'createdAt': cv.created_at.isoformat(), 'updatedAt': cv.updated_at.isoformat(), 'content': read_cv_content(cv.content)}
    if cv.job_title: data['jobTitle'] = cv.job_title
    if cv.company: data['company'] = cv.company
    if cv.match_id: data['matchId'] = cv.match_id
    if cv.source_cv_id: data['sourceCvId'] = cv.source_cv_id
    return data


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


def reject_new_unsafe_links(content: CvContent, inherited: dict | None = None) -> None:
    original_links = inherited.get('links') if inherited else None
    if has_new_unsafe_links(content.links, original_links):
        raise HTTPException(422, 'Contact links cannot use unsafe URL schemes or include credentials.')


@cv_payload_router.post('/cvs', status_code=201)
async def create_cv(data: CvCreateInput, user: CurrentUser):
    source_content = None
    if data.tailorDraftId is not None:
        draft, source = await require_current_draft(data.tailorDraftId, user)
        if data.kind != 'tailored' or data.sourceCvId != str(draft.source_cv_id):
            raise HTTPException(422, 'The tailored CV must use the source CV of the draft.')
        if data.content is None or not data.content.model_fields_set:
            raise HTTPException(422, 'Provide the reviewed CV content.')
        source_content = source.content
    if data.content is not None and data.content.model_fields_set:
        reject_new_unsafe_links(data.content, source_content)
        content = data.content.model_dump(exclude_unset=True)
    else:
        # The browser omits content when creating a CV from the profile.
        # Validate the snapshot before creating a new record.
        try:
            snapshot = CvContent.model_validate(await read_profile(user))
        except ValidationError:
            raise HTTPException(422, 'Your profile exceeds the supported CV limits or contains invalid text. Edit it and try again.') from None
        reject_new_unsafe_links(snapshot)
        content = snapshot.model_dump(exclude_unset=True)
    cv = SavedCv(user_id=user.id, name=data.name or 'Original CV', kind=data.kind or 'original',
                 job_title=data.jobTitle, company=data.company, match_id=data.matchId,
                 source_cv_id=data.sourceCvId, content=content)
    await cv.insert()
    return present_cv(cv)


@router.get('/cvs/{cv_id}')
async def get_cv(cv_id: str, user: CurrentUser):
    cv = await owned_cv(cv_id, user)
    return {**present_cv(cv), 'contentRevision': content_revision(cv.content)}


@cv_payload_router.put('/cvs/{cv_id}')
async def update_cv(cv_id: str, data: CvWriteInput, user: CurrentUser):
    cv = await owned_cv(cv_id, user)
    fields = {
        attr: value for attr, value in [
            ('name', data.name), ('kind', data.kind), ('job_title', data.jobTitle),
            ('company', data.company), ('match_id', data.matchId),
        ] if value is not None
    }
    if data.content is not None:
        reject_new_unsafe_links(data.content, cv.content)
        submitted = data.content.model_dump(exclude_unset=True)
        if read_cv_content(submitted) != read_cv_content(cv.content):
            fields['content'] = submitted
    fields['updated_at'] = datetime.now(UTC)
    await cv.update({'$set': fields})
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
    content = checked_saved_content(cv.content)
    name = content['fullName'] or user.full_name
    email = content['email'] or user.email
    pdf = await render_pdf(render_saved_cv_pdf, name, email, content)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', cv.name).strip('_') or 'CV'
    return Response(
        pdf,
        media_type='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{safe}.pdf"'},
    )


@cv_payload_router.post('/cvs/preview/pdf', response_class=Response,
                        responses={200: {'content': {'application/pdf': {}}}})
async def preview_cv_pdf(data: CvPreviewInput, user: CurrentUser) -> Response:
    """Render unsaved content and verify source drafts without writing records."""
    source = None
    if data.tailorDraftId is not None:
        _, source = await require_current_draft(data.tailorDraftId, user)
    elif data.cvId is not None and any(unsafe_contact_link(link) for link in data.content.links):
        source = await owned_cv(data.cvId, user)
    reject_new_unsafe_links(data.content, source.content if source else None)
    content = data.content.model_dump()
    name = content['fullName'] or user.full_name
    email = content['email'] or user.email
    pdf = await render_pdf(render_saved_cv_pdf, name, email, content)
    return Response(pdf, media_type='application/pdf',
                    headers={'Content-Disposition': 'inline; filename="preview.pdf"'})


router.include_router(cv_payload_router)


class MeUpdate(BaseModel):
    name: AccountName | None = None
    email: EmailStr | None = None
    currentPassword: ExistingPassword | None = None


@auth_payload_router.patch('/me')
async def update_me(data: MeUpdate, user: CurrentUser):
    if data.email and data.email.lower() != user.email:
        if not data.currentPassword or not await verify_password(data.currentPassword, user.hashed_password):
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
    current: ExistingPassword
    next: NewPassword


@auth_payload_router.post('/me/password', status_code=204)
async def change_password(data: PasswordInput, user: CurrentUser, request: Request, response: Response):
    if not await verify_password(data.current, user.hashed_password):
        raise HTTPException(403, 'Incorrect password')
    user.hashed_password = await hash_password(data.next)
    await user.save()
    # Revoke other sessions while keeping this browser signed in.
    await RefreshToken.find(RefreshToken.user_id == user.id).delete()
    await session(user, request, response)



router.include_router(auth_payload_router)
