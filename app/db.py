from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.applications.models import Application, ApplicationEvent
from app.auth.models import AuthRateBucket, RefreshToken
from app.config import settings
from app.gmail.models import GmailConnection, GmailMessage, GmailOAuthState, GmailReview, GmailSuppression, SchedulerLease
from app.integration.models import SavedCv
from app.matches.models import MatchRecord, MatchUsage
from app.tailor.models import TailorDraft
from app.profiles.models import Profile
from app.users.models import User

client: AsyncMongoClient = AsyncMongoClient(settings.mongodb_uri)


async def init_db() -> None:
    await client.admin.command("ping")
    await init_beanie(database=client[settings.mongodb_db_name], document_models=[User, RefreshToken, AuthRateBucket, Profile, SavedCv, Application, MatchRecord, MatchUsage, TailorDraft, ApplicationEvent, GmailConnection, GmailOAuthState, GmailMessage, GmailReview, GmailSuppression, SchedulerLease])
