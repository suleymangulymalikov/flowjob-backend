import os

# Tests use the MONGODB_URI from .env but always a separate database.
os.environ["MONGODB_DB_NAME"] = "flowjob_test"
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-bytes-long")
# Tests must never call the real job-board APIs.
os.environ["JOBS_LIVE_FETCH"] = "false"

import pytest
from fastapi.testclient import TestClient

from app.applications.models import Application
from app.matches.models import MatchRecord, MatchUsage
from app.tailor.models import TailorDraft
from app.auth.models import RefreshToken
from app.integration.models import SavedCv
from app.jobs.models import JobPosting, JobSource
from app.main import app
from app.profiles.models import Profile
from app.users.models import User


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db(client):
    async def clear():
        await User.delete_all()
        await RefreshToken.delete_all()
        await Profile.delete_all()
        await SavedCv.delete_all()
        await Application.delete_all()
        await MatchRecord.delete_all()
        await MatchUsage.delete_all()
        await TailorDraft.delete_all()
        await JobPosting.delete_all()
        await JobSource.delete_all()

    client.portal.call(clear)
