from datetime import datetime
from typing import Annotated

from beanie import Document, Indexed, PydanticObjectId
from pydantic import BaseModel, Field

from app.profiles.schemas import ProfileData


class CvImportMarker(BaseModel):
    """The account's one Smart CV Import into its Profile (POST /api/v1/profile/import)."""

    saved_at: datetime
    request_id: str  # the browser's id for that save, so a retried request gets the same answer


class Profile(Document, ProfileData):
    user_id: Annotated[PydanticObjectId, Indexed(unique=True)]
    frontend_data: dict | None = None
    # Protected: exclude=True keeps the marker out of every Beanie save(), insert() and replace(), and out of every
    # response. Profile saves and account edits save the whole document, so they can neither write nor erase it.
    # It is read like any other field, and only app/cv_import/profile.py sets it, in one atomic update.
    cv_import: CvImportMarker | None = Field(default=None, exclude=True)

    class Settings:
        name = "profiles"
