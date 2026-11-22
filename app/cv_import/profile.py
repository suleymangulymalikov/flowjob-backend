"""Smart CV Import into the user's main Profile, once per account.

GET  /api/v1/profile/import  whether this account can still import, with the Profile (and its revision) the review
                             starts from
POST /api/v1/profile/import  save the reviewed Profile and use up the import, in one atomic write

The PDF itself is read by POST /api/v1/profile/import/pdf (router.py), which stores nothing. So a failed or cancelled
extraction, a discarded review or skipping onboarding never uses up the import: only a successful save here does.

The one-time rule lives in the Profile document itself (Profile.cv_import), written by a single MongoDB update that
also writes the Profile. It applies only while the document has no marker and still holds the Profile the user
reviewed, so of two saves at once exactly one wins, and a review of an outdated Profile never overwrites newer
changes. The other routes that write profiles cannot touch the marker (see Profile.cv_import).

Saved CVs are never created or changed here: a CV made from the Profile later is an independent copy.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from pymongo.errors import DuplicateKeyError

from app.auth.dependencies import get_current_user
from app.cv.content import CvContent, CvPayload
from app.cv.http import CvPayloadRoute
from app.cv.revision import content_revision
from app.integration.router import (
    UIProfile,
    blank_profile,
    native_sections,
    native_to_ui,
    reject_new_unsafe_links,
)
from app.profiles.models import Profile
from app.users.models import User

router = APIRouter(prefix='/api/v1', tags=['cv-import'], route_class=CvPayloadRoute)
CurrentUser = Annotated[User, Depends(get_current_user)]

ALREADY_IMPORTED = (409, ('Your CV has already been imported into your Profile. You can keep editing your Profile '
                          'at any time.'))
PROFILE_CHANGED = (412, ('Your Profile changed while you were reviewing the import. Review it again so nothing you '
                         'saved is lost.'))
# The writes a save may retry after losing a race it did not cause (another write creating or changing the Profile).
_ATTEMPTS = 3


class ImportedProfileSave(CvPayload):
    profile: CvContent
    revision: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]  # GET /profile/import's revision the review used
    requestId: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')]  # the same for every retry of one save


async def _read(user: User) -> tuple[dict | None, dict[str, Any]]:
    """The raw profile document (None if there is none yet) and the Profile the browser sees for it."""
    raw = await Profile.get_pymongo_collection().find_one({'user_id': user.id})
    if raw is None:
        return None, blank_profile(user)
    if raw.get('frontend_data') is not None:
        return raw, raw['frontend_data']
    return raw, native_to_ui(Profile.model_validate(raw), user)


def _marker(raw: dict | None) -> dict | None:
    return raw.get('cv_import') if raw else None


async def import_used(user: User) -> bool:
    return await Profile.get_pymongo_collection().find_one(
        {'user_id': user.id, 'cv_import': {'$ne': None}}, {'_id': 1}) is not None


@router.get('/profile/import')
async def import_status(user: CurrentUser):
    raw, current = await _read(user)
    marker = _marker(raw)
    return {
        'available': marker is None,
        'importedAt': marker['saved_at'].replace(tzinfo=UTC).isoformat() if marker else None,
        'profile': current,
        'revision': content_revision(current),
    }


def _dump(value: Any) -> Any:
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value.model_dump() if hasattr(value, 'model_dump') else value


async def save_imported_profile(user: User, data: ImportedProfileSave) -> dict[str, Any]:
    for _ in range(_ATTEMPTS):
        raw, current = await _read(user)
        marker = _marker(raw)
        if marker is not None:
            if marker.get('request_id') == data.requestId:
                return current  # a retry of the save that already succeeded
            raise HTTPException(*ALREADY_IMPORTED)
        if content_revision(current) != data.revision:
            raise HTTPException(*PROFILE_CHANGED)

        # The account's name and email (or what the user set in the Profile) always win over the PDF's.
        reject_new_unsafe_links(data.profile, current)
        reviewed = data.profile.model_dump()
        reviewed['fullName'] = current.get('fullName') or user.full_name
        reviewed['email'] = current.get('email') or user.email
        profile = UIProfile.model_validate(reviewed)
        saved = profile.model_dump()

        fields = {'frontend_data': saved, **{k: _dump(v) for k, v in native_sections(profile).items()},
                  'cv_import': {'saved_at': datetime.now(UTC), 'request_id': data.requestId}}
        # Applies only to an unmarked document that still holds exactly the Profile just checked.
        query = {'user_id': user.id, 'cv_import': None,
                 'frontend_data': raw.get('frontend_data') if raw else None}
        try:
            result = await Profile.get_pymongo_collection().update_one(query, {'$set': fields}, upsert=raw is None)
        except DuplicateKeyError:
            continue  # another write created this user's profile first: check again
        if result.matched_count or result.upserted_id is not None:
            return saved
        # The document changed between the read and the write: check again.
    raise HTTPException(*PROFILE_CHANGED)


@router.post('/profile/import')
async def save_import(data: ImportedProfileSave, user: CurrentUser):
    """Save the Profile the user reviewed and confirmed. Returns the saved Profile (as GET /api/v1/profile does)."""
    return await save_imported_profile(user, data)
