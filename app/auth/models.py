from datetime import datetime
from typing import Annotated

from beanie import Document, Indexed, PydanticObjectId
from pymongo import IndexModel


class RefreshToken(Document):
    token_hash: Annotated[str, Indexed(unique=True)]
    user_id: PydanticObjectId
    expires_at: datetime

    class Settings:
        name = "refresh_tokens"
        # MongoDB deletes documents automatically once expires_at has passed.
        indexes = [IndexModel("expires_at", expireAfterSeconds=0)]
