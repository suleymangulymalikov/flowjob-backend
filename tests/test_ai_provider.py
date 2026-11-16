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
