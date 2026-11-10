"""AI CV tailoring API tests. OpenAI is always mocked — no key, no network, no charges."""
from beanie import PydanticObjectId
import pytest

from app.config import settings
from app.services import ai_matching, ai_provider, ai_tailoring
from app.services.ai_provider import Usage
from app.services.ai_tailoring import TailoringResult
from app.tailor.models import TailorDraft
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
                           suggested=SUMMARY.removesuffix('.'),
                           explanation='Consistent sentence punctuation.', job_requirement='Backend'),
            ProposedChange(section=ChangeSection.experience, experience_id='0', bullet_index=0, original=BULLET,
                           suggested=BULLET + '.',
                           explanation='Consistent bullet punctuation.'),
        ]
    return TailoringAnalysis(changes=changes, missing_qualifications=['Docker'], clarifying_questions=['Used Docker?'])


@pytest.fixture
def mock_ai(monkeypatch):
    state = {'calls': 0, 'last_sections': None}

    def fake(job_title, company, job_context, cv_sections, _analysis=None):
        state['calls'] += 1
        state['last_sections'] = cv_sections
        return TailoringResult(analysis=_analysis or tailoring(), usage=Usage(600, 300), model='mock')

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
    assert all(s['reviewRequired'] is False for s in d['suggestions'])  # only formatting changed
    assert d['sourceCvRevision'] == client.get(f'{BASE}/cvs/{cv}', headers=h).json()['contentRevision']


def test_pii_minimized_in_sections(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    tailor(client, h, cv)
    s = mock_ai['last_sections']
    assert BULLET in s
    for pii in ('Ana Secret', 'ana@example.com', '555', 'linkedin.com/in/ana'):
        assert pii not in s


def test_invalid_proposals_are_discarded_without_losing_valid_changes(client, monkeypatch):
    proposals = tailoring(changes=[
        ProposedChange(section=ChangeSection.summary, original=original, suggested='Rewrite', explanation='x')
        for original in ('', '  ', 'Engineer', 'Engineer who builds APIs. More details')
    ] + [
        ProposedChange(section=ChangeSection.experience, experience_id=item_id, bullet_index=index,
                       original=BULLET, suggested=BULLET + '.', explanation='x')
        for item_id, index in (('missing', 0), ('0', -1), ('0', 42), (None, 0))
    ] + [
        ProposedChange(section=ChangeSection.project, project_id='absent', original='A REST service',
                       suggested='A REST service.', explanation='x'),
        *tailoring().changes,
        tailoring().changes[0],
    ])
    monkeypatch.setattr(ai_tailoring, 'analyze',
                        lambda *a: TailoringResult(analysis=proposals, usage=Usage(1, 1), model='mock'))
    h = signup(client)
    cv = make_cv(client, h)
    r = tailor(client, h, cv)
    assert r.status_code == 201, r.text
    assert [s['original'] for s in r.json()['suggestions']] == [SUMMARY, BULLET]


@pytest.mark.parametrize('suggested', [
    'Engineer who builds APIs and has an AWS certification.',
    'Engineer who builds APIs with Docker.',
    'Engineer who built 300 APIs.',
    'Engineer who leads the API team at Contoso.',
    'Engineer who does not build APIs.',
])
def test_exact_source_does_not_auto_approve_new_claims(client, monkeypatch, suggested):
    analysis = tailoring(changes=[
        ProposedChange(section=ChangeSection.summary, original=SUMMARY, suggested=suggested,
                       explanation='Claimed to be safe by the model.', review_required=False),
        ProposedChange(section=ChangeSection.skills, original='Python, MongoDB', suggested='MongoDB, Python',
                       explanation='Sort existing skills.', review_required=False),
    ])
    monkeypatch.setattr(ai_tailoring, 'analyze',
                        lambda *a: TailoringResult(analysis=analysis, usage=Usage(1, 1), model='mock'))
    h = signup(client)
    result = tailor(client, h, make_cv(client, h)).json()
    assert [s['reviewRequired'] for s in result['suggestions']] == [True, False]


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
        'sourceCvId': cv_id, 'tailorDraftId': d['id'], 'content': tailored_content}).json()
    assert saved['kind'] == 'tailored' and saved['sourceCvId'] == cv_id
    assert saved['content']['summary'] != SUMMARY  # accepted change applied
    assert saved['content']['experience'][0]['bullets'][0] == BULLET  # rejected change NOT applied
    # Original is unchanged.
    assert client.get(f'{BASE}/cvs/{cv_id}', headers=h).json()['content'] == original
    # A second tailored version from the same original is independent.
    saved2 = client.post(f'{BASE}/cvs', headers=h, json={
        'name': 'Backend v2', 'kind': 'tailored', 'sourceCvId': cv_id, 'tailorDraftId': d['id'], 'content': tailored_content}).json()
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
        'name': 'Edited', 'kind': 'tailored', 'sourceCvId': cv_id, 'tailorDraftId': d['id'], 'content': edited}).json()
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
    (ai_provider.AiTimeout, 504),
    (ai_provider.AiRateLimited, 429),
    (ai_provider.AiInvalidResponse, 502),
    (ai_provider.AiNotConfigured, 503),
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
                        lambda *a, **k: AnalysisResult(analysis=match_analysis, usage=Usage(10, 10), model='mock'))
    monkeypatch.setattr(ai_tailoring, 'analyze',
                        lambda *a, **k: TailoringResult(analysis=tailoring(), usage=Usage(5, 5), model='mock'))
    h = signup(client)
    cv = make_cv(client, h)
    mid = client.post(f'{BASE}/matches', headers=h,
                      json={'cvId': cv, 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}).json()['id']
    r = client.post(f'{BASE}/tailor', headers=h, json={'matchId': mid})
    assert r.status_code == 201 and r.json()['matchId'] == mid and r.json()['suggestions']
    # A match belonging to another user cannot be used.
    bob = signup(client, 'bob@example.com')
    assert client.post(f'{BASE}/tailor', headers=bob, json={'matchId': mid}).status_code == 404


def test_direct_draft_is_retrievable_by_id_without_ai_or_match(client, mock_ai):
    """Direct tailoring (no matchId) leaves a draft that GET /tailor/{id} can re-read after a refresh."""
    h = signup(client)
    cv = make_cv(client, h)
    created = tailor(client, h, cv, company='Acme')
    assert created.status_code == 201 and created.json()['matchId'] is None
    assert mock_ai['calls'] == 1
    strip = lambda d: {k: v for k, v in d.items() if k != 'createdAt'}  # tz formatting differs after a DB round-trip
    for _ in range(2):  # reopening/refreshing the draft route
        got = client.get(f'{BASE}/tailor/{created.json()["id"]}', headers=h)
        assert got.status_code == 200 and strip(got.json()) == strip(created.json())
    assert mock_ai['calls'] == 1  # reads never call the AI provider
    assert client.get(f'{BASE}/matches', headers=h).json() == []  # no match record was created
    # Another user cannot read it; unknown ids are a plain 404.
    bob = signup(client, 'bob@example.com')
    assert client.get(f'{BASE}/tailor/{created.json()["id"]}', headers=bob).status_code == 404
    assert client.get(f'{BASE}/tailor/000000000000000000000000', headers=h).status_code == 404


@pytest.mark.parametrize('change', ['reorder', 'edit', 'other-section'])
def test_stale_draft_cannot_preview_or_save(client, mock_ai, change):
    h = signup(client)
    cv = make_cv(client, h)
    endpoint = f'{BASE}/cvs/{cv}'
    content = client.get(endpoint, headers=h).json()['content']
    content['experience'][0]['bullets'].append('Maintained the test suite')
    assert client.put(endpoint, headers=h, json={'content': content}).status_code == 200
    draft = tailor(client, h, cv).json()
    reviewed = _apply_accepted(content, draft['suggestions'], {'s0', 's1'})
    payload = {'content': reviewed, 'tailorDraftId': draft['id']}
    preview = client.post(f'{BASE}/cvs/preview/pdf', headers=h, json=payload)
    assert preview.status_code == 200 and preview.content.startswith(b'%PDF')
    assert len(client.get(f'{BASE}/cvs', headers=h).json()) == 1  # preview/download is unsaved
    if change == 'reorder':
        content['experience'][0]['bullets'].reverse()
    elif change == 'edit':
        content['experience'][0]['bullets'][0] = 'Built Python command-line tools'
    else:
        content['location'] = 'Warsaw'  # Even a field omitted from the AI prompt changes the revision.
    assert client.put(endpoint, headers=h, json={'content': content}).status_code == 200
    assert client.post(f'{BASE}/cvs/preview/pdf', headers=h, json=payload).status_code == 409
    assert client.post(f'{BASE}/cvs', headers=h, json={**payload, 'sourceCvId': cv, 'kind': 'tailored'}).status_code == 409
    assert client.get(endpoint, headers=h).json()['content'] == content
    assert len(client.get(f'{BASE}/cvs', headers=h).json()) == 1
    assert mock_ai['calls'] == 1  # viewing or rejecting never regenerates
    regenerated = tailor(client, h, cv).json()
    assert regenerated['id'] != draft['id'] and regenerated['sourceCvRevision'] != draft['sourceCvRevision']


def test_identical_cvs_do_not_share_drafts_and_cannot_swap_sources(client, mock_ai):
    h = signup(client)
    cv_a = make_cv(client, h, 'CV A')
    cv_b = make_cv(client, h, 'CV B')
    a = tailor(client, h, cv_a).json()
    b = tailor(client, h, cv_b).json()
    assert a['sourceCvRevision'] == b['sourceCvRevision']
    assert a['id'] != b['id'] and b['sourceCvId'] == cv_b
    content = client.get(f'{BASE}/cvs/{cv_b}', headers=h).json()['content']
    payload = {'tailorDraftId': a['id'], 'content': content, 'kind': 'tailored', 'sourceCvId': cv_b}
    assert client.post(f'{BASE}/cvs', headers=h, json=payload).status_code == 422
    other = signup(client, 'other@example.com')
    assert client.post(f'{BASE}/cvs/preview/pdf', headers=other, json=payload).status_code == 404
    assert client.post(f'{BASE}/cvs', headers=other, json=payload).status_code == 404


def test_legacy_draft_remains_readable_but_cannot_be_applied(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    draft = tailor(client, h, cv).json()

    async def remove_revision():
        await TailorDraft.get_pymongo_collection().update_one(
            {'_id': PydanticObjectId(draft['id'])}, {'$unset': {'source_cv_revision': ''}},
        )

    client.portal.call(remove_revision)
    response = client.get(f'{BASE}/tailor/{draft["id"]}', headers=h)
    assert response.status_code == 200
    assert response.json()['sourceCvRevision'] is None
    assert all(s['reviewRequired'] for s in response.json()['suggestions'])
    payload = {'tailorDraftId': draft['id'], 'content': {'summary': SUMMARY}}
    assert client.post(f'{BASE}/cvs/preview/pdf', headers=h, json=payload).status_code == 409
    assert client.post(f'{BASE}/cvs', headers=h, json={**payload, 'sourceCvId': cv, 'kind': 'tailored'}).status_code == 409
    assert mock_ai['calls'] == 1


    created = tailor(client, h, cv).json()
    assert created['id'] != draft['id'] and created['sourceCvRevision']
    assert created['suggestions'][1]['original'] == BULLET
    assert mock_ai['calls'] == 2


def test_new_job_draft_has_revision_and_target_metadata(client, mock_ai):
    assert client.get(f'{BASE}/tailor/capabilities').json() == {'sourceVerification': 1}
    h = signup(client)
    cv = make_cv(client, h)
    previous = tailor(client, h, cv).json()
    r = tailor(client, h, cv, jobTitle='Product Quality Analyst', company='Example Analytics',
               jobDescription='Product analyst. ' + JOB)
    assert r.status_code == 201, r.text
    created = r.json()
    source = client.get(f'{BASE}/cvs/{cv}', headers=h).json()
    assert created['id'] != previous['id']
    assert created['sourceCvId'] == cv and created['sourceCvRevision'] == source['contentRevision']
    assert created['suggestions'][1] == {
        'id': 's1', 'section': 'experience', 'original': BULLET, 'suggested': BULLET + '.',
        'explanation': 'Consistent bullet punctuation.', 'reviewRequired': False,
        'experienceId': '0', 'bulletIndex': 0,
    }
    retrieved = client.get(f'{BASE}/tailor/{created["id"]}', headers=h).json()
    for key in ('id', 'sourceCvId', 'sourceCvRevision', 'jobTitle', 'company', 'suggestions'):
        assert retrieved[key] == created[key]

    reviewed = _apply_accepted(source['content'], created['suggestions'], {'s0', 's1'})
    payload = {'content': reviewed, 'tailorDraftId': created['id']}
    pdf = client.post(f'{BASE}/cvs/preview/pdf', headers=h, json=payload)
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')
    assert len(client.get(f'{BASE}/cvs', headers=h).json()) == 1
    saved = client.post(f'{BASE}/cvs', headers=h, json={**payload, 'kind': 'tailored', 'sourceCvId': cv})
    assert saved.status_code == 201 and saved.json()['id'] != cv
    assert saved.json()['content'] == reviewed
    assert client.get(f'{BASE}/cvs/{cv}', headers=h).json()['content'] == source['content']
    assert mock_ai['calls'] == 2


def test_company_changes_do_not_reuse_a_different_jobs_draft(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    old = tailor(client, h, cv, company='Example One').json()
    new = tailor(client, h, cv, company='Example Two').json()
    assert new['id'] != old['id'] and new['company'] == 'Example Two'
    assert tailor(client, h, cv, company='Example Two').json()['id'] == new['id']
    assert mock_ai['calls'] == 2


def test_explicit_fresh_request_bypasses_verified_cache(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    first = tailor(client, h, cv).json()
    assert client.get(f'{BASE}/tailor/{first["id"]}', headers=h).status_code == 200
    assert tailor(client, h, cv).json()['id'] == first['id']
    assert mock_ai['calls'] == 1
    fresh = tailor(client, h, cv, fresh=True).json()
    assert fresh['id'] != first['id'] and fresh['sourceCvRevision'] == first['sourceCvRevision']
    assert mock_ai['calls'] == 2


def test_verified_draft_with_old_job_fingerprint_remains_readable_but_is_not_reused(client, mock_ai):
    from hashlib import sha256

    h = signup(client)
    cv = make_cv(client, h)
    draft = tailor(client, h, cv).json()
    old_job_hash = sha256(f'backend engineer\n{JOB}'.encode()).hexdigest()

    async def use_legacy_key():
        await TailorDraft.get_pymongo_collection().update_one(
            {'_id': PydanticObjectId(draft['id'])}, {'$set': {'job_hash': old_job_hash}},
        )

    client.portal.call(use_legacy_key)
    original = client.get(f'{BASE}/tailor/{draft["id"]}', headers=h).json()
    assert original['sourceCvRevision'] == draft['sourceCvRevision']
    replacement = tailor(client, h, cv).json()
    assert replacement['id'] != draft['id'] and mock_ai['calls'] == 2
    assert client.get(f'{BASE}/tailor/{draft["id"]}', headers=h).json() == original


def test_job_url_separates_direct_drafts_and_is_preserved_on_retrieval(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    old = tailor(client, h, cv, jobUrl='https://example.com/jobs/1').json()
    url = 'https://example.com/jobs/2'
    new = tailor(client, h, cv, jobUrl=url).json()
    assert new['id'] != old['id'] and new['jobUrl'] == url
    assert tailor(client, h, cv, jobUrl=url).json()['id'] == new['id']
    assert client.get(f'{BASE}/tailor/{new["id"]}', headers=h).json()['jobUrl'] == url
    assert mock_ai['calls'] == 2


def test_tailoring_keeps_direct_drafts_and_each_match_identity_separate(client, mock_ai):
    from app.tailor.router import job_context_from_match

    h = signup(client)
    cv = make_cv(client, h)
    job_title = 'Backend Engineer'
    company = 'Example'
    url = 'https://example.com/jobs/1'
    body = {'cvId': cv, 'jobTitle': job_title, 'jobDescription': JOB, 'company': company, 'jobUrl': url}
    first = client.post(f'{BASE}/matches', headers=h, json=body).json()
    other_job = {**body, 'jobDescription': JOB + ' Second posting for a separate team.'}
    second = client.post(f'{BASE}/matches', headers=h, json=other_job).json()
    assert first['id'] != second['id']

    # The built-in matching mock returns the same analysis for both jobs. Also give
    # direct tailoring the identical text, so the identity must come from the match ID.
    context = job_context_from_match(first)
    assert context == job_context_from_match(second)
    direct = tailor(client, h, cv, company=company, jobUrl=url, jobDescription=context).json()
    linked = []
    for match in (first, second):
        request = {'matchId': match['id']}
        response = client.post(f'{BASE}/tailor', headers=h, json=request)
        assert response.status_code == 201
        draft = response.json()
        assert (draft['sourceCvId'], draft['matchId'], draft['jobUrl']) == (cv, match['id'], url)
        assert client.post(f'{BASE}/tailor', headers=h, json=request).json()['id'] == draft['id']
        linked.append(draft['id'])
    assert direct['matchId'] is None and len({direct['id'], *linked}) == 3
    assert mock_ai['calls'] == 3

    other = signup(client, 'other@example.com')
    other_cv = make_cv(client, other)
    other_draft = tailor(client, other, other_cv, company=company, jobUrl=url, jobDescription=context).json()
    assert other_draft['id'] != direct['id'] and other_draft['sourceCvId'] == other_cv
    assert client.post(f'{BASE}/tailor', headers=other, json={'matchId': first['id']}).status_code == 404
    assert mock_ai['calls'] == 4

    first_url = f'{BASE}/matches/{first["id"]}'
    assert client.delete(first_url, headers=h).status_code == 204
    assert client.post(f'{BASE}/tailor', headers=h, json={'matchId': first['id']}).status_code == 404
    assert client.get(f'{BASE}/tailor/{linked[0]}', headers=h).status_code == 200
    assert tailor(client, h, cv, company=company, jobUrl=url, jobDescription=context).json()['id'] == direct['id']

    assert client.delete(f'{BASE}/cvs/{cv}', headers=h).status_code == 204
    assert client.post(f'{BASE}/tailor', headers=h, json={'matchId': second['id']}).status_code == 404
    assert tailor(client, h, cv, company=company, jobUrl=url, jobDescription=context).status_code == 404
    assert client.get(f'{BASE}/tailor/{linked[1]}', headers=h).status_code == 200
    assert mock_ai['calls'] == 4
