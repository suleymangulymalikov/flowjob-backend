from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.profiles.models import Profile
from app.profiles.schemas import ProfileData, ProfileUpdate
from app.users.models import User

router = APIRouter(prefix="/profile", tags=["profile"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("", response_model=ProfileData)
async def read_profile(user: CurrentUser) -> Profile:
    # A user without a saved profile gets an empty one.
    return await Profile.find_one(Profile.user_id == user.id) or Profile(user_id=user.id)


@router.patch("", response_model=ProfileData)
async def update_profile(data: ProfileUpdate, user: CurrentUser) -> Profile:
    profile = await Profile.find_one(Profile.user_id == user.id) or Profile(user_id=user.id)
    for section in ProfileUpdate.model_fields:
        value = getattr(data, section)
        if value is not None:
            setattr(profile, section, value)
    await profile.save()
    return profile
