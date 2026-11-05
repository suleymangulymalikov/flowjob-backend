"""UI-compatible persisted CVs; ownership is enforced in every route."""
from datetime import UTC, datetime
from typing import Annotated, Any
from beanie import Document, Indexed, PydanticObjectId
from pydantic import Field

class SavedCv(Document):
    user_id: Annotated[PydanticObjectId, Indexed()]
    name: str = 'Original CV'
    kind: str = 'original'
    job_title: str | None = None
    company: str | None = None
    match_id: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = 'saved_cvs'
