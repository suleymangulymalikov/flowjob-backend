"""AI CV tailoring API tests. OpenAI is always mocked — no key, no network, no charges."""
import pytest

from app.config import settings
from app.services import ai_matching, ai_tailoring
from app.services.ai_matching import Usage
from app.services.ai_tailoring import TailoringResult
from app.tailor.schemas import ChangeSection, ProposedChange, TailoringAnalysis

BASE = '/api/v1'
JOB = (
    'Backend Engineer. Required: Python, REST APIs, MongoDB. Preferred: Docker. '
    'You will build and maintain services and write tests.'
)
SUMMARY = 'Engineer who builds APIs.'
BULLET = 'Built REST APIs with Python and MongoDB'


def signup(client, email='ana@example.com'):
    r = client.post(f'{BASE}/auth/register', json={'name': 'Ana', 'email': email, 'password': 'secret123'})
    assert r.status_code == 201, r.text
    return {'Authorization': f"Bearer {r.json()['accessToken']}"}


def make_cv(client, headers, name='My CV'):
    content = {
        'fullName': 'Ana Secret', 'email': 'ana@example.com', 'phone': '555', 'location': 'Boston',
        'links': ['linkedin.com/in/ana'], 'summary': SUMMARY,
        'experience': [{'id': '0', 'title': 'Developer', 'company': 'Acme', 'start': '2022-01', 'end': '',
                        'bullets': [BULLET]}],
        'education': [], 'projects': [{'id': '0', 'name': 'FlowJob', 'tech': 'FastAPI', 'description': 'A REST service'}],
        'skills': ['Python', 'MongoDB'], 'languages': '',
    }
    r = client.post(f'{BASE}/cvs', json={'name': name, 'kind': 'original', 'content': content}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()['id']


def tailoring(changes=None):
    if changes is None:
        changes = [
            ProposedChange(section=ChangeSection.summary, original=SUMMARY,
                           suggested='Backend engineer who builds Python and MongoDB REST APIs.',
                           explanation='Leads with backend.', job_requirement='Backend'),
            ProposedChange(section=ChangeSection.experience, experience_id='0', bullet_index=0, original=BULLET,
                           suggested='Designed and built REST APIs in Python backed by MongoDB',
                           explanation='Sharper verb; facts unchanged.'),
        ]
    return TailoringAnalysis(changes=changes, missing_qualifications=['Docker'], clarifying_questions=['Used Docker?'])


@pytest.fixture
def mock_ai(monkeypatch):
    state = {'calls': 0, 'last_sections': None}

    def fake(job_title, company, job_context, cv_sections, _analysis=None):
        state['calls'] += 1
        state['last_sections'] = cv_sections
        return TailoringResult(analysis=_analysis or tailoring(), usage=Usage(600, 300), model=settings.openai_model)

    monkeypatch.setattr(ai_tailoring, 'analyze', fake)
    return state


def tailor(client, headers, cv_id, **kw):
    body = {'cvId': cv_id, 'jobTitle': 'Backend Engineer', 'jobDescription': JOB, **kw}
    return client.post(f'{BASE}/tailor', json=body, headers=headers)


# --- auth / validation / ownership -----------------------------------------------

def test_requires_auth(client):
    assert client.post(f'{BASE}/tailor', json={'cvId': 'x', 'jobTitle': 'Dev', 'jobDescription': JOB}).status_code == 401


def test_invalid_input(client, mock_ai):
    h = signup(client)
    assert client.post(f'{BASE}/tailor', json={}, headers=h).status_code == 422  # nothing provided
    cv = make_cv(client, h)
    assert client.post(f'{BASE}/tailor', json={'cvId': cv, 'jobTitle': 'Dev', 'jobDescription': 'short'}, headers=h).status_code == 422


def test_cv_ownership(client, mock_ai):
    ana = signup(client, 'ana@example.com')
    ana_cv = make_cv(client, ana)
    bob = signup(client, 'bob@example.com')
    assert tailor(client, bob, ana_cv).status_code == 404


def test_invalid_ids(client, mock_ai):
    h = signup(client)
    assert client.post(f'{BASE}/tailor', json={'cvId': 'not-an-id', 'jobTitle': 'Dev', 'jobDescription': JOB}, headers=h).status_code == 422
    assert client.get(f'{BASE}/tailor/not-an-id', headers=h).status_code == 404


# --- success / grounding / unsupported -------------------------------------------

def test_successful_tailoring(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    r = tailor(client, h, cv)
    assert r.status_code == 201, r.text
    d = r.json()
    assert len(d['suggestions']) == 2
    assert d['suggestions'][0]['section'] == 'summary' and d['suggestions'][0]['original'] == SUMMARY
    assert d['missingQualifications'] == ['Docker'] and d['questions']
    assert d['tokens'] == {'input': 600, 'output': 300} and d['estimatedCostUsd'] > 0
    assert all(s['reviewRequired'] is False for s in d['suggestions'])  # both grounded


def test_pii_minimized_in_sections(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    tailor(client, h, cv)
    s = mock_ai['last_sections']
    assert BULLET in s
    for pii in ('Ana Secret', 'ana@example.com', '555', 'linkedin.com/in/ana'):
        assert pii not in s


def test_ungrounded_change_is_flagged_for_review(client, monkeypatch):
    fabricated = tailoring(changes=[
        ProposedChange(section=ChangeSection.experience, experience_id='0', bullet_index=0,
                       original='Led a team of 50 engineers and raised revenue 300%',  # not in the CV
                       suggested='Led a large team', explanation='x'),
    ])
    monkeypatch.setattr(ai_tailoring, 'analyze',
                        lambda *a, **k: TailoringResult(analysis=fabricated, usage=Usage(1, 1), model=settings.openai_model))
    h = signup(client)
    cv = make_cv(client, h)
    d = tailor(client, h, cv).json()
    assert d['suggestions'][0]['reviewRequired'] is True  # fabricated original -> flagged


# --- original unchanged / save / multiple versions / source deletion -------------

def _apply_accepted(original_content, suggestions, accepted_ids):
    """Mimic the frontend applying only accepted suggestions to a clone."""
    import copy
    c = copy.deepcopy(original_content)
    for s in suggestions:
        if s['id'] not in accepted_ids:
            continue
        if s['section'] == 'summary':
            c['summary'] = s['suggested']
        elif s['section'] == 'experience':
            c['experience'][int(s['experienceId'])]['bullets'][s['bulletIndex']] = s['suggested']
    return c


def test_save_creates_independent_tailored_cv_and_leaves_original_unchanged(client, mock_ai):
    h = signup(client)
    cv_id = make_cv(client, h)
    original = client.get(f'{BASE}/cvs/{cv_id}', headers=h).json()['content']
    d = tailor(client, h, cv_id).json()
    # Accept only the summary suggestion (reject the experience one).
    tailored_content = _apply_accepted(original, d['suggestions'], {'s0'})
    saved = client.post(f'{BASE}/cvs', headers=h, json={
        'name': 'Backend v1', 'kind': 'tailored', 'jobTitle': 'Backend Engineer', 'company': 'Acme',
        'sourceCvId': cv_id, 'content': tailored_content}).json()
    assert saved['kind'] == 'tailored' and saved['sourceCvId'] == cv_id
    assert saved['content']['summary'] != SUMMARY  # accepted change applied
    assert saved['content']['experience'][0]['bullets'][0] == BULLET  # rejected change NOT applied
    # Original is unchanged.
    assert client.get(f'{BASE}/cvs/{cv_id}', headers=h).json()['content'] == original
    # A second tailored version from the same original is independent.
    saved2 = client.post(f'{BASE}/cvs', headers=h, json={
        'name': 'Backend v2', 'kind': 'tailored', 'sourceCvId': cv_id, 'content': tailored_content}).json()
    assert saved2['id'] != saved['id']
    assert len(client.get(f'{BASE}/cvs', headers=h).json()) == 3  # original + 2 tailored


def test_saving_an_edited_suggestion(client, mock_ai):
    h = signup(client)
    cv_id = make_cv(client, h)
    original = client.get(f'{BASE}/cvs/{cv_id}', headers=h).json()['content']
    d = tailor(client, h, cv_id).json()
    edited = __import__('copy').deepcopy(original)
    edited['summary'] = 'My own edited summary.'  # user edited the suggestion before saving
    saved = client.post(f'{BASE}/cvs', headers=h, json={
        'name': 'Edited', 'kind': 'tailored', 'sourceCvId': cv_id, 'content': edited}).json()
    assert saved['content']['summary'] == 'My own edited summary.'


def test_tailored_cv_survives_source_deletion(client, mock_ai):
    h = signup(client)
    cv_id = make_cv(client, h)
    content = client.get(f'{BASE}/cvs/{cv_id}', headers=h).json()['content']
    saved = client.post(f'{BASE}/cvs', headers=h, json={
        'name': 'Backend v1', 'kind': 'tailored', 'sourceCvId': cv_id, 'content': content}).json()
    assert client.delete(f'{BASE}/cvs/{cv_id}', headers=h).status_code == 204
    # The tailored CV remains usable after the original is deleted.
    got = client.get(f'{BASE}/cvs/{saved["id"]}', headers=h)
    assert got.status_code == 200 and got.json()['sourceCvId'] == cv_id


# --- provider errors / quota / dedup / match reuse -------------------------------

@pytest.mark.parametrize('exc,code', [
    (ai_tailoring.AiTimeout, 504),
    (ai_tailoring.AiRateLimited, 429),
    (ai_tailoring.AiInvalidResponse, 502),
    (ai_tailoring.AiNotConfigured, 503),
])
def test_provider_errors_mapped(client, monkeypatch, exc, code):
    monkeypatch.setattr(ai_tailoring, 'analyze', lambda *a, **k: (_ for _ in ()).throw(exc('x')))
    h = signup(client)
    cv = make_cv(client, h)
    assert tailor(client, h, cv).status_code == code


def test_quota_enforced(client, mock_ai, monkeypatch):
    monkeypatch.setattr(settings, 'tailor_daily_quota', 1)
    h = signup(client)
    cv = make_cv(client, h)
    assert tailor(client, h, cv, jobDescription=f'{JOB} a').status_code == 201
    assert tailor(client, h, cv, jobDescription=f'{JOB} b').status_code == 429


def test_dedup_cache(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    r1 = tailor(client, h, cv)
    r2 = tailor(client, h, cv)
    assert r1.json()['id'] == r2.json()['id'] and mock_ai['calls'] == 1


def test_match_reuse_path(client, monkeypatch):
    # Create a match (mocked), then tailor from it without a job description.
    from app.services.ai_matching import AnalysisResult
    from app.matches.schemas import AiAnalysis, EvidenceStatus, Importance, RequirementAssessment
    match_analysis = AiAnalysis(enough_info=True, overall_assessment='Good fit.', requirements=[
        RequirementAssessment(requirement='Python', importance=Importance.required,
                              status=EvidenceStatus.clearly_demonstrated, evidence='Built APIs'),
    ])
    monkeypatch.setattr(ai_matching, 'analyze',
                        lambda *a, **k: AnalysisResult(analysis=match_analysis, usage=Usage(10, 10), model=settings.openai_model))
    monkeypatch.setattr(ai_tailoring, 'analyze',
                        lambda *a, **k: TailoringResult(analysis=tailoring(), usage=Usage(5, 5), model=settings.openai_model))
    h = signup(client)
    cv = make_cv(client, h)
    mid = client.post(f'{BASE}/matches', headers=h,
                      json={'cvId': cv, 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}).json()['id']
    r = client.post(f'{BASE}/tailor', headers=h, json={'matchId': mid})
    assert r.status_code == 201 and r.json()['matchId'] == mid and r.json()['suggestions']
    # A match belonging to another user cannot be used.
    bob = signup(client, 'bob@example.com')
    assert client.post(f'{BASE}/tailor', headers=bob, json={'matchId': mid}).status_code == 404
