from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.auth.models import RefreshToken
from app.config import settings
from app.integration.models import SavedCv
from app.profiles.models import Profile
from app.users.models import User

client: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri)


async def init_db() -> None:
    await client.admin.command("ping")
    await init_beanie(database=client[settings.mongodb_db_name], document_models=[User, RefreshToken, Profile, SavedCv])
