from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings

client: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri)


async def init_db() -> None:
    await client.admin.command("ping")
    await init_beanie(database=client[settings.mongodb_db_name], document_models=[])
