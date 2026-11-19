"""Mock AI (OPENAI_API_KEY=mock) is for tests and local development only: opt-in, refused in production, and loud."""
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import (
    MockAiNotAllowed,
    Settings,
    check_mock_ai,
    mock_ai_problem,
    settings,
)
from app.services import ai_gmail, ai_matching, ai_provider, ai_tailoring
from app.services.ai_provider import AiNotConfigured

ROOT = Path(__file__).resolve().parents[1]
BASE = '/api/v1'
JOB = ('We are hiring a Backend Engineer. Required: Python, REST APIs, MongoDB. '
       'Preferred: Docker and CI/CD. You will build and maintain services and write tests.')


def config(**overrides) -> Settings:
    values = {'mongodb_uri': 'mongodb://127.0.0.1:1', 'jwt_secret': 'fixture-jwt', **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def warnings_log(caplog):
    logger = logging.getLogger('uvicorn.error')
    attach = not logger.propagate  # uvicorn may disable propagation; then attach the capture handler directly
    if attach:
        logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger='uvicorn.error')
    yield caplog
    if attach:
        logger.removeHandler(caplog.handler)


# --- configuration rules -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('key', [None, '', 'sk-fixture-not-a-real-key'])
def test_real_or_missing_key_is_never_mock(key):
    for allowed in (False, True):
        for secure in (False, True):
            cfg = config(openai_api_key=key, allow_mock_ai=allowed, cookie_secure=secure)
            assert not cfg.mock_ai
            assert mock_ai_problem(cfg) is None
            check_mock_ai(cfg)


def test_mock_key_without_explicit_opt_in_is_refused():
    cfg = config(openai_api_key='mock', allow_mock_ai=False)
    with pytest.raises(MockAiNotAllowed, match='ALLOW_MOCK_AI=true'):
        check_mock_ai(cfg)


def test_mock_is_refused_with_production_cookie_settings_even_when_allowed():
    cfg = config(openai_api_key='mock', allow_mock_ai=True, cookie_secure=True)
    with pytest.raises(MockAiNotAllowed, match='COOKIE_SECURE=true'):
        check_mock_ai(cfg)


def test_mock_runs_only_with_opt_in_on_a_non_production_configuration():
    cfg = config(openai_api_key='mock', allow_mock_ai=True, cookie_secure=False)
    assert cfg.mock_ai
    check_mock_ai(cfg)


def test_opt_in_alone_does_not_turn_mock_on():
    assert not config(openai_api_key='sk-fixture-not-a-real-key', allow_mock_ai=True).mock_ai


@pytest.mark.parametrize('extra, message', [
    ({}, 'ALLOW_MOCK_AI=true'),
    ({'ALLOW_MOCK_AI': 'true', 'COOKIE_SECURE': 'true'}, 'COOKIE_SECURE=true'),
])
def test_backend_refuses_to_start_and_never_prints_configuration_values(tmp_path, extra, message):
    secret_uri = 'mongodb://fixture-user:fixture-db-password@127.0.0.1:1'
    secret_jwt = 'fixture-jwt-secret-value'
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('MONGODB_', 'OPENAI_', 'JWT_', 'ALLOW_MOCK_AI', 'COOKIE_', 'GOOGLE_', 'GMAIL_'))}
    env.update({'MONGODB_URI': secret_uri, 'JWT_SECRET': secret_jwt, 'OPENAI_API_KEY': 'mock',
                'PYTHONPATH': str(ROOT), **extra})
    result = subprocess.run([sys.executable, '-c', 'import app.config'], check=False, cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert 'MockAiNotAllowed' in output and message in output
    assert 'fixture-db-password' not in output and secret_jwt not in output


def test_backend_starts_with_a_real_key_shape_and_production_settings(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('MONGODB_', 'OPENAI_', 'JWT_', 'ALLOW_MOCK_AI', 'COOKIE_', 'GOOGLE_', 'GMAIL_'))}
    env.update({'MONGODB_URI': 'mongodb://127.0.0.1:1', 'JWT_SECRET': 'fixture', 'COOKIE_SECURE': 'true',
                'OPENAI_API_KEY': 'sk-fixture-not-a-real-key', 'PYTHONPATH': str(ROOT)})
    result = subprocess.run([sys.executable, '-c', 'import app.config'], check=False, cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# --- runtime re-check and warnings -------------------------------------------------------------------------------

@pytest.fixture
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(ai_provider, '_mock_warned', set())


def test_each_workflow_warns_once_that_results_are_canned(fresh_warnings, warnings_log):
    ai_matching.analyze('Dev', None, JOB, {'skills': ['Python']})
    ai_matching.analyze('Dev', None, JOB, {'skills': ['Python']})
    ai_tailoring.analyze('Dev', None, JOB, 'CV text')
    ai_gmail.analyze('email')
    canned = [r.getMessage() for r in warnings_log.records if 'MOCK AI MODE' in r.getMessage()]
    assert canned == [
        'MOCK AI MODE: Job Matching is returning canned results, not OpenAI output.',
        'MOCK AI MODE: Tailoring is returning canned results, not OpenAI output.',
        'MOCK AI MODE: Gmail analysis is returning canned results, not OpenAI output.',
    ]


def test_startup_warning_when_mock_mode_is_on(warnings_log):
    ai_provider.log_mock_ai_status()
    assert any('MOCK AI MODE is ON' in r.getMessage() for r in warnings_log.records)


def test_no_startup_warning_with_a_real_key(monkeypatch, warnings_log):
    monkeypatch.setattr(settings, 'openai_api_key', 'sk-fixture-not-a-real-key')
    ai_provider.log_mock_ai_status()
    assert not any('MOCK AI' in r.getMessage() for r in warnings_log.records)


@pytest.mark.parametrize('change', [{'allow_mock_ai': False}, {'cookie_secure': True}])
@pytest.mark.parametrize('call', [
    lambda: ai_matching.analyze('Dev', None, JOB, {'skills': ['Python']}),
    lambda: ai_tailoring.analyze('Dev', None, JOB, 'CV text'),
    lambda: ai_gmail.analyze('email'),
])
def test_disallowed_mock_at_runtime_fails_instead_of_inventing_results(monkeypatch, warnings_log, change, call):
    for name, value in change.items():
        monkeypatch.setattr(settings, name, value)
    with pytest.raises(AiNotConfigured):
        call()
    assert any('Mock AI refused' in r.getMessage() for r in warnings_log.records)


def test_disallowed_mock_returns_503_from_job_matching(client, monkeypatch):
    r = client.post(f'{BASE}/auth/register', json={'name': 'Ana', 'email': 'ana@example.com', 'password': 'secret123'})
    headers = {'Authorization': f"Bearer {r.json()['accessToken']}"}
    content = {'fullName': 'Ana', 'email': 'ana@example.com', 'phone': '', 'location': '', 'links': [],
               'summary': 'Engineer.', 'experience': [], 'education': [], 'projects': [],
               'skills': ['Python'], 'languages': ''}
    cv = client.post(f'{BASE}/cvs', json={'name': 'CV', 'kind': 'original', 'content': content}, headers=headers)
    monkeypatch.setattr(settings, 'allow_mock_ai', False)
    response = client.post(f'{BASE}/matches', headers=headers,
                           json={'cvId': cv.json()['id'], 'jobTitle': 'Dev', 'jobDescription': JOB})
    assert response.status_code == 503
    assert 'score' not in response.json()
