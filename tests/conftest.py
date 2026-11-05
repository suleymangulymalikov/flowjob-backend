import os

# Tests use the MONGODB_URI from .env but always a separate database.
os.environ["MONGODB_DB_NAME"] = "flowjob_test"
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-bytes-long")

import pytest
from fastapi.testclient import TestClient

from app.auth.models import RefreshToken
from app.integration.models import SavedCv
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

    client.portal.call(clear)
