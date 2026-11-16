"""The real SDK runs against an in-memory transport. No provider network is used."""

import json

import httpx
import openai
import pytest

from app.config import settings
from app.services import ai_matching, ai_tailoring
from tests.test_matches import BASE, JOB, make_cv, signup


@pytest.mark.parametrize('path,provider', [('matches', ai_matching), ('tailor', ai_tailoring)])
@pytest.mark.parametrize('failure,status,detail', [
    ('timeout', 504, 'The analysis timed out. Please try again.'),
    ('connection', 502, 'The AI service is unavailable. Please try again.'),
    ('rate-limit', 429, 'The AI service is busy. Please try again shortly.'),
    ('provider-503', 502, 'The AI service is unavailable. Please try again.'),
    ('bad-key', 503, 'The AI service is not set up correctly. Please contact support.'),
    ('unusable', 502, 'The AI service returned an unusable result.'),
])
def test_one_sdk_attempt_per_generation_and_explicit_retry_after_failure(
    client, monkeypatch, path, provider, failure, status, detail,
):
    real_sdk = openai.OpenAI
    requests = []

    def respond(request):
        assert request.url.host == 'openai.test'
        assert request.url.path == '/v1/responses'
        payload = json.loads(request.content)
        assert payload['store'] is False
        requests.append(request.url.path)
        output = []
        if len(requests) == 1:
            if failure == 'timeout':
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            if failure == 'connection':
                raise httpx.ConnectError('synthetic disconnect', request=request)
            if failure in ('rate-limit', 'provider-503', 'bad-key'):
                upstream = {'rate-limit': 429, 'bad-key': 401}.get(failure, 503)
                return httpx.Response(upstream, json={'error': {'message': 'synthetic provider error'}})
        else:
            analysis = provider._mock_result().analysis
            output = [{
                'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': analysis.model_dump_json(), 'annotations': []}],
            }]
        return httpx.Response(200, json={
            'id': 'resp_fixture', 'object': 'response', 'model': settings.openai_model,
            'status': 'completed', 'output': output,
            'usage': {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10},
        })

    def sdk(**kwargs):
        assert kwargs['max_retries'] == 0
        return real_sdk(
            **kwargs,
            base_url='https://openai.test/v1',
            http_client=httpx.Client(transport=httpx.MockTransport(respond), trust_env=False),
        )

    monkeypatch.setattr(openai, 'OpenAI', sdk)
    monkeypatch.setattr(settings, 'openai_api_key', 'synthetic-fixture-key')
    monkeypatch.setattr(settings, 'ai_global_daily_quota', 1)
    headers = signup(client)
    body = {'cvId': make_cv(client, headers), 'jobTitle': 'Dev', 'jobDescription': JOB}

    failed = client.post(f'{BASE}/{path}', headers=headers, json=body)
    assert failed.status_code == status and failed.json() == {'detail': detail}
    assert len(requests) == 1

    # The first failure refunded its reservation. Only a new explicit request reaches the SDK.
    succeeded = client.post(f'{BASE}/{path}', headers=headers, json=body)
    assert succeeded.status_code == 201
    assert succeeded.json()['tokens'] == {'input': 7, 'output': 3}
    assert len(requests) == 2
    assert client.post(f'{BASE}/{path}', headers=headers, json=body).json()['id'] == succeeded.json()['id']
    assert client.post(f'{BASE}/{path}', headers=headers, json={**body, 'jobTitle': 'Different role'}).status_code == 503
    assert len(requests) == 2


# --- privacy-safe failure logging ---------------------------------------------------------------------------------

SECRET_TEXT = 'Dear Ada Example, private interview notes'  # stands in for an email body, CV text or prompt
SECRET_KEY = 'sk-proj-synthetic-secret-value'


@pytest.fixture
def ai_log(caplog):
    import logging
    logger = logging.getLogger('uvicorn.error')
    attach = not logger.propagate  # uvicorn may disable propagation; then attach the capture handler directly
    if attach:
        logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger='uvicorn.error')
    yield caplog
    if attach:
        logger.removeHandler(caplog.handler)


def _sdk_answering(monkeypatch, response):
    real_sdk = openai.OpenAI

    def sdk(**kwargs):
        return real_sdk(**kwargs, base_url='https://openai.test/v1',
                        http_client=httpx.Client(transport=httpx.MockTransport(response), trust_env=False))

    monkeypatch.setattr(openai, 'OpenAI', sdk)
    monkeypatch.setattr(settings, 'openai_api_key', SECRET_KEY)


@pytest.mark.parametrize('status,code,logged_code', [
    (400, 'invalid_request_error', 'invalid_request_error'),
    (401, 'invalid_api_key', 'invalid_api_key'),
    (500, 'not a <plain> code', None),  # anything that is not a plain identifier is dropped
])
def test_provider_failures_log_fixed_fields_only(monkeypatch, ai_log, status, code, logged_code):
    from app.services import ai_gmail
    from app.services.ai_provider import AiError

    def respond(request):
        assert SECRET_TEXT in request.content.decode()
        message = f'Invalid input "{SECRET_TEXT}" for key {SECRET_KEY}'  # a provider echoing the request
        return httpx.Response(status, headers={'x-request-id': 'req_abc123'},
                              json={'error': {'message': message, 'type': 'invalid_request_error', 'code': code}})

    _sdk_answering(monkeypatch, respond)
    with pytest.raises(AiError):
        ai_gmail.analyze(f'Subject: Interview\n\n{SECRET_TEXT}')

    lines = [r.getMessage() for r in ai_log.records if r.getMessage().startswith('AI request failed')]
    assert len(lines) == 1
    line = lines[0]
    assert f'status={status}' in line and 'request_id=req_abc123' in line
    assert (f'code={logged_code}' in line) if logged_code else ('code=' not in line)
    for leaked in (SECRET_TEXT, 'Ada', 'interview notes', SECRET_KEY, 'synthetic-secret', 'Invalid input', '<plain>'):
        assert leaked not in ai_log.text


def test_unusable_model_output_is_not_logged(monkeypatch, ai_log):
    from app.services import ai_gmail
    from app.services.ai_provider import AiInvalidResponse

    def respond(request):
        # Output that fails schema validation: pydantic's own message would quote it.
        bad = json.dumps({'is_recruitment': SECRET_TEXT, 'items': SECRET_TEXT})
        return httpx.Response(200, json={
            'id': 'resp_fixture', 'object': 'response', 'model': settings.openai_model, 'status': 'completed',
            'output': [{'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': bad, 'annotations': []}]}],
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
        })

    _sdk_answering(monkeypatch, respond)
    with pytest.raises(AiInvalidResponse):
        ai_gmail.analyze(SECRET_TEXT)

    assert 'AI request failed' in ai_log.text and 'ValidationError' in ai_log.text
    for leaked in (SECRET_TEXT, 'Ada', 'input_value', SECRET_KEY):
        assert leaked not in ai_log.text
