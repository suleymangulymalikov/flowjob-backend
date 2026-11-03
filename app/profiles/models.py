from typing import Annotated

from beanie import Document, Indexed, PydanticObjectId

from app.profiles.schemas import ProfileData


class Profile(Document, ProfileData):
    user_id: Annotated[PydanticObjectId, Indexed(unique=True)]

    class Settings:
        name = "profiles"
