import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]

# A child pytest invocation must reject the configuration before application
# imports, even if someone later moves the guard out of pytest_sessionstart.
COLLECT = """
import sys
import pytest

class BlockAppImport:
    def find_spec(self, fullname, *args):
        if fullname == "app" or fullname.startswith("app."):
            raise AssertionError("application import reached")

sys.meta_path.insert(0, BlockAppImport())
sys.exit(pytest.main(["--collect-only", "-q", "tests/test_matches.py"]))
"""


@pytest.mark.parametrize(
    ("overrides", "option", "message"),
    [
        ({}, None, "Set FLOWJOB_TEST_MONGOD"),
        ({}, "/nonexistent/flowjob-test-mongod", "executable was not found"),
        ({"MONGODB_URI": "mongodb+srv://example.invalid"}, "mongod", "External MongoDB"),
        ({"MONGODB_URI": "mongodb://192.0.2.10:27017"}, "mongod", "External MongoDB"),
        ({"MONGODB_URI": "mongodb://fixture-user:fixture-password@127.0.0.1:27017"}, "mongod", "External MongoDB"),
        ({"MONGODB_URI": "mongodb://localhost:27017"}, "mongod", "External MongoDB"),
        ({"mongodb_uri": ""}, "mongod", "External MongoDB"),
        ({"MONGODB_DB_NAME": "flowjob_test"}, "mongod", "External MongoDB"),
        ({}, "/usr/bin/true", "did not start on its private Unix socket"),
    ],
)
def test_rejects_unsafe_configuration_before_application_import(overrides, option, message):
    environ = {
        key: value for key, value in os.environ.items()
        if key.upper() not in {"MONGODB_URI", "MONGODB_DB_NAME", "FLOWJOB_TEST_MONGOD", "PYTEST_ADDOPTS"}
    }
    environ.update(overrides)
    if option is not None:
        environ["FLOWJOB_TEST_MONGOD"] = option
    result = subprocess.run(
        [sys.executable, "-c", COLLECT], env=environ, cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert message in output
    assert "application import reached" not in output
    assert all(value not in output for value in overrides.values() if value)


def test_application_uses_generated_database_and_mock_openai(client):
    from app.config import settings

    assert re.fullmatch(r"flowjob_test_[0-9a-f]{32}", settings.mongodb_db_name)
    assert settings.mongodb_uri.startswith("mongodb://%2F")
    assert settings.openai_api_key == "mock"
    assert client.get("/health").status_code == 200
