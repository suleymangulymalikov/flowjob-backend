"""Focused integration tests: the AI-first pipeline behind the existing Job Match API."""
from app.config import settings
from app.ai_matching.matching import Citation
from app.services import ai_matching
from app.services.ai_matching import AnalysisResult, Usage
from tests.test_matches import BASE, JOB, analysis, make_cv, mock_ai, req, signup  # noqa: F401


def fake(monkeypatch, result):
    monkeypatch.setattr(ai_matching, 'analyze', lambda *a, **k: AnalysisResult(
        analysis=result, usage=Usage(input_tokens=400, output_tokens=200), model='mock'))


def post(client, headers, cv, job=JOB):
    return client.post(f'{BASE}/matches', headers=headers,
                       json={'cvId': cv, 'jobTitle': 'Backend Engineer', 'jobDescription': job})


def test_an_invented_requirement_is_rejected_at_the_api(client, monkeypatch):
    base = analysis()
    base.requirements.append(req('inv', 'Kubernetes', 'required', 'not_shown',
                                 source_quote='Experience with Kubernetes clusters'))
    fake(monkeypatch, base)
    h = signup(client)
    m = post(client, h, make_cv(client, h)).json()
    assert m['score'] == 80  # the invented requirement changed nothing
    assert m['rejected'] == [{'id': 'inv', 'requirement': 'Kubernetes', 'reason': 'quote_not_in_job',
                              'quote': 'Experience with Kubernetes clusters'}]
    assert all(x['requirement'] != 'Kubernetes' for x in m['missing'])


def test_a_working_condition_never_reaches_the_score_at_the_api(client, monkeypatch):
    job = JOB + ' Comfortable working remotely with a distributed team.'
    base = analysis()
    base.requirements.append(req('rw', 'Comfortable working remotely', 'required', 'not_shown',
                                 source_quote='Comfortable working remotely with a distributed team'))
    fake(monkeypatch, base)
    h = signup(client)
    m = post(client, h, make_cv(client, h), job).json()
    assert m['score'] == 80 and m['rejected'][0]['reason'] == 'working_condition'


def test_a_false_citation_earns_nothing_at_the_api(client, monkeypatch):
    lying = analysis(reqs=[
        req('r1', 'Python', 'required', 'demonstrated', [Citation(path='skills', quote='Python')]),
        req('r2', 'MongoDB', 'required', 'demonstrated', [Citation(path='skills', quote='Rust')]),  # false quote
        req('r3', 'Docker', 'preferred', 'demonstrated', [Citation(path='projects.9.name', quote='Docker')]),
    ])
    fake(monkeypatch, lying)
    h = signup(client)
    m = post(client, h, make_cv(client, h)).json()
    assert m['score'] == 40  # only Python survives: required 1/2 -> 100*(60*0.5)/75
    assert sorted(x['requirement'] for x in m['missing']) == ['Docker', 'MongoDB']
    assert all(x['evidence'] == 'Not shown in your CV.' for x in m['missing'])


def test_legacy_fields_and_standard_category_for_old_clients(client, monkeypatch):
    base = analysis()
    base.requirements.append(req('s1', 'REST APIs', 'standard', 'demonstrated',
                                 [Citation(path='experience.0.bullets.0', quote='Built REST APIs')]))
    fake(monkeypatch, base)
    h = signup(client)
    m = post(client, h, make_cv(client, h)).json()
    rest = next(x for x in m['matched'] if x['requirement'] == 'REST APIs')
    assert 'importance' not in rest and rest['category'] == 'standard'  # no wrong Required/Preferred tag
    python = next(x for x in m['matched'] if x['requirement'] == 'Python')
    assert python['importance'] == 'required' and python['status'] == 'clearly_demonstrated'
    assert m['jobText'] == '' and {'strengths', 'gaps', 'summary', 'scoreExplanation'} <= set(m)


def test_quota_refund_and_cache_still_hold_with_the_new_pipeline(client, monkeypatch, mock_ai):
    monkeypatch.setattr(settings, 'match_daily_quota', 2)
    h = signup(client)
    cv = make_cv(client, h)
    first = post(client, h, cv)
    assert first.status_code == 201 and post(client, h, cv).json()['id'] == first.json()['id']
    assert mock_ai['calls'] == 1  # cache hit under the ai-first prompt version


def test_match_generation_uses_the_larger_budget_longer_timeout_and_zero_temperature(monkeypatch):
    from app.ai_matching.matching import MatchAnalysis
    seen = {}

    def fake_request(system, user, schema, **kwargs):
        seen.update(schema=schema, **kwargs)
        return MatchAnalysis(enough_info=False), Usage(1, 1)

    monkeypatch.setattr(settings, 'openai_api_key', 'synthetic-key')
    monkeypatch.setattr(ai_matching, 'request_analysis', fake_request)
    ai_matching.analyze('Dev', None, JOB, {'skills': ['Python']})
    assert seen == {'schema': MatchAnalysis, 'max_output_tokens': 3000, 'timeout': 60.0, 'temperature': 0.0}


def test_a_model_that_rejects_temperature_is_retried_once_without_it(monkeypatch):
    import httpx
    import openai
    from app.ai_matching.matching import MatchAnalysis
    bodies = []

    def respond(request):
        import json
        body = json.loads(request.content)
        bodies.append(body)
        if 'temperature' in body:
            return httpx.Response(400, json={'error': {'message': "Unsupported parameter: 'temperature'", 'type': 'invalid_request_error'}})
        text = MatchAnalysis(enough_info=False).model_dump_json()
        return httpx.Response(200, json={
            'id': 'r', 'object': 'response', 'model': 'gpt-4o-mini', 'status': 'completed',
            'output': [{'type': 'message', 'id': 'm', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
            'usage': {'input_tokens': 5, 'output_tokens': 2, 'total_tokens': 7}})

    real = openai.OpenAI
    monkeypatch.setattr(openai, 'OpenAI', lambda **kw: real(
        **kw, base_url='https://openai.test/v1', http_client=httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)))
    monkeypatch.setattr(settings, 'openai_api_key', 'synthetic-key')
    result = ai_matching.analyze('Dev', None, JOB, {'skills': ['Python']})
    assert [('temperature' in b) for b in bodies] == [True, False] and result.usage.output_tokens == 2
    assert all(b['max_output_tokens'] == 3000 for b in bodies)
