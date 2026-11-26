import os
import sys
from contextlib import ExitStack, chdir
from secrets import token_hex

import pytest

from tests.mongo_isolation import IsolationError, TestMongo, require_mongod, start_mongod

MONGO = pytest.StashKey[TestMongo]()


def pytest_sessionstart(session):
    # Collection imports app modules. Establish the isolated settings before
    # collection starts and refuse to continue if a plugin already imported them.
    config = session.config
    resources = ExitStack()
    config.add_cleanup(resources.close)
    try:
        executable = require_mongod(os.environ)
        if any(name == "app" or name.startswith("app.") for name in sys.modules):
            raise IsolationError("Application code was imported before test isolation was established.")
        mongo = resources.enter_context(start_mongod(executable))
        env = resources.enter_context(pytest.MonkeyPatch.context())
        for key in list(os.environ):
            upper = key.upper()
            if upper in {"OPENAI_API_KEY", "JWT_SECRET", "FRONTEND_BASE_URL"} or upper.startswith(("GOOGLE_", "GMAIL_")):
                env.delenv(key)
        env.setenv("MONGODB_URI", mongo.uri)
        env.setenv("MONGODB_DB_NAME", mongo.database)
        env.setenv("OPENAI_API_KEY", "mock")
        # Mock AI is opt-in and refused with production settings (app.config.check_mock_ai).
        env.setenv("ALLOW_MOCK_AI", "true")
        env.setenv("COOKIE_SECURE", "false")
        env.setenv("JWT_SECRET", token_hex(32))
        # Gmail stays unconfigured and the scheduler off unless a test opts in explicitly.
        env.setenv("GMAIL_SCHEDULER_ENABLED", "false")
        # Tests never call the real job-board APIs.
        env.setenv("JOBS_LIVE_FETCH", "false")

        # Settings loads .env relative to the working directory at import time.
        # Load it from the empty test directory, then restore the project cwd.
        with chdir(mongo.directory):
            from app.config import settings

        if (settings.mongodb_uri, settings.mongodb_db_name, settings.openai_api_key) != (
            mongo.uri, mongo.database, "mock"
        ):
            raise IsolationError("Application settings do not match the verified test environment.")
        config.stash[MONGO] = mongo
    except IsolationError as error:
        pytest.exit(str(error), returncode=pytest.ExitCode.USAGE_ERROR)


@pytest.fixture(scope="session")
def _running_client(pytestconfig):
    from fastapi.testclient import TestClient

    from app.db import client as mongo_client
    from app.main import app

    database = mongo_client.get_database(pytestconfig.stash[MONGO].database)
    with TestClient(app) as http:
        yield http, database


@pytest.fixture
def client(_running_client):
    http, database = _running_client

    async def clear():
        for name in await database.list_collection_names():
            if not name.startswith("system."):
                await database[name].delete_many({})

    http.portal.call(clear)
    return http
