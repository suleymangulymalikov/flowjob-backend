"""AI CV tailoring API tests. OpenAI is always mocked — no key, no network, no charges."""
from beanie import PydanticObjectId
import pytest

from app.config import settings
from app.services import ai_matching, ai_provider, ai_tailoring
from app.services.ai_provider import Usage
from app.services.ai_tailoring import TailoringResult
from app.tailor.models import TailorDraft
from app.tailor.schemas import ProposedChange, TailoringAnalysis

BASE = '/api/v1'
JOB = (
    'Backend Engineer. Required: Python, REST APIs, MongoDB. Preferred: Docker. '
    'You will build and maintain services and write tests.'
)
SUMMARY = 'Engineer who builds APIs.'
BULLET = 'Built REST APIs with Python and MongoDB'
NEW_SUMMARY = 'Backend engineer who builds REST APIs with Python and MongoDB.'
NEW_BULLET = 'Designed and shipped REST APIs with Python and MongoDB'


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


def rewrite(path, text, reason='Clearer for this job.'):
    return ProposedChange(op='rewrite', path=path, suggested=text, reason=reason, job_requirement='Backend')


def tailoring(changes=None):
    if changes is None:
        changes = [
            rewrite('summary', NEW_SUMMARY, 'Leads with the backend focus the job asks for.'),
            rewrite('experience.0.bullets.0', NEW_BULLET),
            ProposedChange(op='reorder', path='skills', order_skills=['MongoDB', 'Python'], reason='Puts the data skill first.'),
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
    assert len(d['suggestions']) == 3
    first = d['suggestions'][0]
    assert (first['section'], first['op'], first['original'], first['suggested']) == ('summary', 'rewrite', SUMMARY, NEW_SUMMARY)
    assert d['missingQualifications'] == [] and d['questions']  # a direct draft makes no 'not evidenced' claims
    assert d['tokens'] == {'input': 600, 'output': 300} and d['estimatedCostUsd'] > 0
    # Only the verified skills reorder may be previewed automatically; wording changes always need approval.
    assert [(s['op'], s['risk'], s['reviewRequired']) for s in d['suggestions']] == [
        ('rewrite', 'review', True), ('rewrite', 'review', True), ('reorder', 'auto', False)]
    assert d['suggestions'][2]['suggested'] == 'MongoDB, Python' and d['suggestions'][2]['original'] == 'Python, MongoDB'
    assert d['proposalStats'] == {'proposed': 3, 'kept': 3, 'rejected': {}, 'rejectedExamples': []}
    assert d['sourceCvRevision'] == client.get(f'{BASE}/cvs/{cv}', headers=h).json()['contentRevision']


def test_pii_minimized_in_sections(client, mock_ai):
    h = signup(client)
    cv = make_cv(client, h)
    tailor(client, h, cv)
    s = mock_ai['last_sections']
    assert BULLET in s
    for pii in ('Ana Secret', 'ana@example.com', '555', 'linkedin.com/in/ana'):
        assert pii not in s


def test_invalid_proposals_are_counted_and_valid_changes_survive(client, monkeypatch):
    bad = [
        rewrite('summary', '   '),                                              # empty_suggestion
        rewrite('summary', SUMMARY.removesuffix('.').upper()),                  # cosmetic_no_change
        rewrite('experience.missing.bullets.0', 'x'),                           # unknown_target
        rewrite('experience.0.bullets.42', 'x'),                                # unknown_target
        rewrite('projects.absent.description', 'x'),                            # unknown_target
        rewrite('education.0.degree', 'x'),                                     # unknown_path
        rewrite('skills', 'Python, Rust'),                                      # rewrite_target_not_text
        ProposedChange(op='reorder', path='skills', order_skills=['Rust'], reason='x'),                    # bad_permutation (names no real skill)
        ProposedChange(op='reorder', path='skills', order_skills=['Python', 'MongoDB'], reason='x'),       # no_change
        ProposedChange(op='reorder', path='experience.0', order_bullets=[0, 0], reason='x'),               # a single bullet cannot be reordered
        ProposedChange(op='delete', path='experience.0.bullets.9', reason='x'),                            # unknown_target
        ProposedChange(op='add_skill', path='skills', suggested='Rust', reason='x'),                       # no_evidence
        rewrite('summary', NEW_SUMMARY),                                        # valid
        rewrite('summary', 'A second change to the same summary.'),            # duplicate_target
        rewrite('experience.0.bullets.0', NEW_BULLET),                          # valid
    ]
    monkeypatch.setattr(ai_tailoring, 'analyze',
                        lambda *a: TailoringResult(analysis=TailoringAnalysis.model_construct(
                            changes=bad, missing_qualifications=[], clarifying_questions=[]), usage=Usage(1, 1), model='mock'))
    h = signup(client)
    cv = make_cv(client, h)
    r = tailor(client, h, cv)
    assert r.status_code == 201, r.text
    body = r.json()
    assert [s['suggested'] for s in body['suggestions']] == [NEW_SUMMARY, NEW_BULLET]
    stats = body['proposalStats']
    assert (stats['proposed'], stats['kept']) == (15, 2)
    assert stats['rejected'] == {
        'empty_suggestion': 1, 'cosmetic_no_change': 1, 'unknown_target': 5, 'unknown_path': 1, 'rewrite_target_not_text': 1,
        'bad_permutation': 1, 'no_change': 1, 'no_evidence': 1, 'duplicate_target': 1}


def _tailor_with(client, monkeypatch, changes, cv_content=None):
    monkeypatch.setattr(ai_tailoring, 'analyze', lambda *a: TailoringResult(
        analysis=TailoringAnalysis(changes=changes), usage=Usage(1, 1), model='mock'))
    h = signup(client)
    cv = make_cv(client, h)
    if cv_content is not None:
        assert client.put(f'{BASE}/cvs/{cv}', headers=h, json={'content': cv_content}).status_code == 200
    return tailor(client, h, cv).json()


def test_the_model_cannot_make_a_rewrite_automatic_only_verified_reorders_are(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [
        rewrite('summary', 'Engineer who builds APIs and has an AWS certification.'),
        rewrite('experience.0.bullets.0', 'Built 300 REST APIs with Python and MongoDB'),
        ProposedChange(op='reorder', path='skills', order_skills=['mongodb', 'PYTHON'], reason='Order.'),
    ])
    # The invented figure (300) is refused outright; the unverifiable claim still needs the user's approval.
    assert [(s['op'], s['risk']) for s in d['suggestions']] == [('rewrite', 'review'), ('reorder', 'auto')]
    assert d['proposalStats']['rejected'] == {'adds_number': 1}
    assert d['proposalStats']['rejectedExamples'] == [
        {'op': 'rewrite', 'path': 'experience.0.bullets.0', 'reason': 'adds_number'}]
    assert d['suggestions'][1]['suggested'] == 'MongoDB, Python'   # original spelling restored


def test_bullet_reorder_must_be_an_exact_permutation_and_is_auto(client, monkeypatch):
    content = {'fullName': 'Ana', 'summary': SUMMARY, 'links': [], 'education': [], 'languages': '',
               'experience': [{'id': '0', 'title': 'Developer', 'company': 'Acme', 'start': '2022-01', 'end': '',
                               'bullets': ['First', 'Second', 'Third']}],
               'projects': [], 'skills': ['Python']}
    d = _tailor_with(client, monkeypatch, [
        ProposedChange(op='reorder', path='experience.0', order_bullets=[2, 0, 1], reason='Most relevant first.'),
        ProposedChange(op='reorder', path='experience.0', order_bullets=[7, 9], reason='names no real bullet'),
    ], content)
    (only,) = d['suggestions']
    assert only['order'] == [2, 0, 1] and only['risk'] == 'auto' and only['experienceId'] == '0' and 'bulletIndex' not in only
    assert d['proposalStats']['rejected'] == {'bad_permutation': 1}


def test_an_incomplete_reorder_is_repaired_into_a_true_permutation_never_adding_or_losing_anything(client, monkeypatch):
    content = {'fullName': 'Ana', 'summary': SUMMARY, 'links': [], 'education': [], 'languages': '', 'projects': [],
               'experience': [{'id': '0', 'title': 'Developer', 'company': 'Acme', 'start': '2022-01', 'end': '',
                               'bullets': ['First', 'Second', 'Third']}],
               'skills': ['MS Office', 'Excel', 'excel', 'Power BI', 'SQL']}
    d = _tailor_with(client, monkeypatch, [
        # The model repeats an index and forgets one; it names a skill the CV lacks and forgets two it has.
        ProposedChange(op='reorder', path='experience.0', order_bullets=[2, 2, 5], reason='Most relevant first.'),
        ProposedChange(op='reorder', path='skills', order_skills=['sql', 'Tableau', 'Power BI', 'Excel'], reason='x'),
    ], content)
    bullets, skills = d['suggestions']
    assert bullets['order'] == [2, 0, 1] and bullets['suggested'] == 'Third\nFirst\nSecond'
    assert skills['suggested'] == 'SQL, Power BI, Excel, MS Office, excel' and skills['risk'] == 'auto'
    assert sorted(skills['suggested'].split(', ')) == sorted(content['skills'])


def test_delete_always_needs_approval(client, monkeypatch):
    content = {'fullName': 'Ana', 'summary': SUMMARY, 'links': [], 'education': [], 'languages': '', 'projects': [],
               'experience': [{'id': '0', 'title': 'Developer', 'company': 'Acme', 'start': '2022-01', 'end': '',
                               'bullets': ['Built APIs', 'Built APIs.']}], 'skills': ['Python', 'python ']}
    d = _tailor_with(client, monkeypatch, [
        ProposedChange(op='delete', path='experience.0.bullets.1', reason='Repeats the first bullet.'),
        ProposedChange(op='delete', path='skills', suggested='python', reason='Duplicate skill.'),
    ], content)
    assert [(s['op'], s['risk'], s['reviewRequired']) for s in d['suggestions']] == [('delete', 'review', True)] * 2
    assert d['suggestions'][0]['original'] == 'Built APIs.' and d['suggestions'][1]['skill'] == 'Python'


@pytest.mark.parametrize('change, reason', [
    (dict(suggested='Docker', evidence_path='projects.0.description', evidence_quote='A REST service'), 'evidence_does_not_name_skill'),
    (dict(suggested='Docker', evidence_path='projects.0.description', evidence_quote='Dockerised everything'), 'evidence_not_in_cv'),
    (dict(suggested='FastAPI', evidence_path='skills', evidence_quote='Python'), 'no_evidence'),
    (dict(suggested='FastAPI', evidence_path='summary', evidence_quote='Engineer who builds APIs'), 'no_evidence'),
    (dict(suggested='Python', evidence_path='projects.0.tech', evidence_quote='FastAPI'), 'already_listed'),
    (dict(suggested='FastAPI'), 'no_evidence'),
])
def test_added_skills_need_a_verbatim_cv_quotation_that_names_them(client, monkeypatch, change, reason):
    d = _tailor_with(client, monkeypatch, [ProposedChange(op='add_skill', path='skills', reason='Job asks for it.', **change)])
    assert d['suggestions'] == [] and d['proposalStats']['rejected'] == {reason: 1}


def test_an_evidence_backed_skill_is_proposed_for_approval_with_its_evidence(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [ProposedChange(
        op='add_skill', path='skills', suggested='FastAPI', reason='The job names FastAPI and your project uses it.',
        evidence_path='projects.0.name', evidence_quote='FastAPI')], {
        'fullName': 'Ana', 'summary': SUMMARY, 'links': [], 'education': [], 'languages': '', 'experience': [],
        'projects': [{'id': '0', 'name': 'API', 'tech': 'FastAPI, MongoDB', 'description': 'A REST service'}],
        'skills': ['Python']})
    (s,) = d['suggestions']
    assert (s['op'], s['risk'], s['skill'], s['suggested']) == ('add_skill', 'review', 'FastAPI', 'Python, FastAPI')
    assert s['evidence'] == {'path': 'projects.0.tech', 'quote': 'FastAPI'}   # wrong field corrected within the same project


def test_a_real_but_non_naming_quote_resolves_to_the_field_of_the_same_project_that_names_the_skill(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [ProposedChange(
        op='add_skill', path='skills', suggested='Power BI', reason='The job prefers Power BI and your project uses it.',
        evidence_path='projects.0.name', evidence_quote='Sales Insights')], {
        'fullName': 'Ana', 'summary': SUMMARY, 'links': [], 'education': [], 'languages': '', 'experience': [],
        'projects': [
            {'id': '0', 'name': 'Sales Insights', 'tech': 'Power BI, SQL', 'description': 'Built a dashboard on sales data'},
            {'id': '1', 'name': 'Other', 'tech': 'Tableau', 'description': 'Built dashboards'}],
        'skills': ['Python']})
    (s,) = d['suggestions']
    assert s['evidence'] == {'path': 'projects.0.tech', 'quote': 'Power BI, SQL'} and s['suggested'] == 'Python, Power BI'


def test_the_naming_fallback_never_crosses_into_another_project(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [ProposedChange(
        op='add_skill', path='skills', suggested='Tableau', reason='x',
        evidence_path='projects.0.name', evidence_quote='Sales Insights')], {
        'fullName': 'Ana', 'summary': SUMMARY, 'links': [], 'education': [], 'languages': '', 'experience': [],
        'projects': [{'id': '0', 'name': 'Sales Insights', 'tech': 'Power BI', 'description': 'Dashboards'},
                     {'id': '1', 'name': 'Other', 'tech': 'Tableau', 'description': 'Built dashboards'}],
        'skills': ['Python']})
    assert d['suggestions'] == [] and d['proposalStats']['rejected'] == {'evidence_does_not_name_skill': 1}


def test_zero_suggestions_is_a_valid_successful_draft(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [])
    assert d['suggestions'] == [] and d['proposalStats'] == {'proposed': 0, 'kept': 0, 'rejected': {}, 'rejectedExamples': []}
    assert d['id'] and d['sourceCvRevision']


def test_match_context_for_ai_first_reports_carries_categories_statuses_evidence_and_duties():
    from app.tailor.router import job_context_from_match
    context = job_context_from_match({
        'overallAssessment': 'Your CV clearly shows QA.',
        'requirements': [
            {'requirement': 'QA on annotated data', 'category': 'required', 'status': 'demonstrated',
             'evidence': [{'path': 'experience.exp-1.bullets.0', 'quote': 'Reviewed 300+ annotated text samples'}]},
            {'requirement': 'SQL', 'category': 'standard', 'status': 'not_shown', 'evidence': []},
        ],
        'excluded': [{'text': 'Build dashboards', 'reason': 'job_duty'}, {'text': 'Remote', 'reason': 'working_condition'}],
    })
    assert '- [required | demonstrated] QA on annotated data — evidence experience.exp-1.bullets.0: "Reviewed 300+' in context
    assert '- [standard | not shown] SQL' in context and 'None' not in context
    assert 'Role duties (context only' in context and '- Build dashboards' in context and 'Remote' not in context


def test_missing_qualifications_come_from_the_match_report_only():
    from app.tailor.router import missing_from_match
    assert missing_from_match({'requirements': [
        {'requirement': 'Python', 'status': 'demonstrated'}, {'requirement': 'SQL pipelines', 'status': 'partial'},
        {'requirement': 'LLM evaluation', 'status': 'not_shown'}]}) == ['SQL pipelines', 'LLM evaluation']
    assert missing_from_match({'partiallyMatched': [{'requirement': 'A'}], 'missing': [{'requirement': 'B'}]}) == ['A', 'B']
    assert missing_from_match({}) == []


def test_match_context_for_historical_reports_keeps_the_legacy_fallback():
    from app.tailor.router import job_context_from_match
    context = job_context_from_match({
        'overallAssessment': 'The candidate fits.',
        'matched': [{'requirement': 'Python', 'importance': 'required'}], 'missing': [{'requirement': 'Docker'}]})
    assert '- [already matched] Python (required)' in context and '- [not shown in CV] Docker' in context and 'None' not in context


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
    from tests.test_matches import analysis
    match_analysis = analysis()
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
    assert r.json()['missingQualifications'] == ['Docker']  # from the match report's verified statuses, not the model
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
    preview = {'tailorDraftId': a['id'], 'content': content}
    assert client.post(f'{BASE}/cvs/preview/pdf', headers=other, json=preview).status_code == 404
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
        'id': 's1', 'section': 'experience', 'op': 'rewrite', 'original': BULLET, 'suggested': NEW_BULLET,
        'newTerms': ['designed', 'shipped'],   # words of the rewrite that are nowhere in the CV
        'explanation': 'Clearer for this job.', 'reviewRequired': True, 'risk': 'review', 'jobRequirement': 'Backend',
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


# --- round 1 of the matching/tailoring fixes ---------------------------------------

NO_SUMMARY_CV = {
    'fullName': 'Ana', 'summary': '', 'links': [], 'education': [], 'languages': '', 'projects': [],
    'experience': [{'id': '0', 'title': 'Developer', 'company': 'Acme', 'start': '2022-01', 'end': '2023-12',
                    'bullets': ['Built REST APIs used by 40,000 customers', 'Wrote tests']}],
    'skills': ['Python']}


def test_a_cv_without_a_summary_can_be_offered_one_built_from_its_own_facts(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [
        rewrite('summary', 'Developer with 2 years of experience building REST APIs for 40.000 customers.')], NO_SUMMARY_CV)
    (s,) = d['suggestions']   # "2 years" is on the CV's own dates line and 40,000 is in a bullet: neither is invented
    assert (s['section'], s['op'], s['original'], s['risk'], s['reviewRequired']) == ('summary', 'rewrite', '', 'review', True)


def test_a_rewrite_with_a_figure_the_cv_does_not_contain_is_refused(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [
        rewrite('summary', 'Developer with 7 years of experience.'),
        rewrite('experience.0.bullets.1', 'Wrote tests, raising coverage to 95%'),
        rewrite('experience.0.bullets.0', 'Built and maintained REST APIs used by 40,000 customers'),
    ], NO_SUMMARY_CV)
    assert [s['suggested'] for s in d['suggestions']] == ['Built and maintained REST APIs used by 40,000 customers']
    assert d['proposalStats']['rejected'] == {'adds_number': 2}


def test_tailoring_from_a_match_reads_the_post_only_when_it_is_the_post_that_match_analysed(client, monkeypatch):
    from app.services.ai_matching import AnalysisResult
    from tests.test_matches import analysis
    seen = []
    match_analysis = analysis()
    monkeypatch.setattr(ai_matching, 'analyze',
                        lambda *a, **k: AnalysisResult(analysis=match_analysis, usage=Usage(10, 10), model='mock'))

    def fake(job_title, company, job_context, cv_sections):
        seen.append(job_context)
        return TailoringResult(analysis=tailoring(), usage=Usage(5, 5), model='mock')

    monkeypatch.setattr(ai_tailoring, 'analyze', fake)
    h = signup(client)
    cv = make_cv(client, h)
    mid = client.post(f'{BASE}/matches', headers=h,
                      json={'cvId': cv, 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}).json()['id']
    assert client.post(f'{BASE}/tailor', headers=h, json={'matchId': mid, 'jobDescription': f'  {JOB}  '}).status_code == 201
    assert client.post(f'{BASE}/tailor', headers=h, json={'matchId': mid, 'jobDescription': JOB + ' Also Kubernetes.'}).status_code == 201
    assert client.post(f'{BASE}/tailor', headers=h, json={'matchId': mid}).status_code == 201
    assert JOB in seen[0] and "employer's own words" in seen[0]
    assert len(seen) == 2 and JOB not in seen[1] and 'Kubernetes' not in seen[1]   # a different post is ignored; no post reuses that draft

    async def stored():
        return [d.model_dump_json() async for d in TailorDraft.find_all()]

    assert all('write tests' not in raw for raw in client.portal.call(stored))   # the post itself is never stored


def test_the_tailoring_model_sees_role_dates_and_total_experience(client, mock_ai):
    h = signup(client)
    assert tailor(client, h, make_cv(client, h)).status_code == 201
    assert '[experience.0.dates] Jan 2022 to present (' in mock_ai['last_sections']
    assert '[total_experience] ' in mock_ai['last_sections']


# --- truthfulness guards (measured against real model output) ----------------------

TEACHER_CV = {
    'fullName': 'Anna', 'summary': 'English teacher with six years of classroom experience.', 'links': [], 'education': [],
    'languages': 'Polish, English', 'projects': [], 'skills': ['Excel', 'Trello'],
    'experience': [{'id': 't', 'title': 'English Teacher', 'company': 'School', 'start': '2019-09', 'end': '2025-08', 'bullets': [
        'Prepared weekly sales reports in Excel', 'Made reports in Excel every week for the sales team',
        'Organised the annual school trip to London for 40 students', 'Ran weekly meetings of the language department']}]}


def test_a_bullet_is_only_removed_when_it_really_repeats_another(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [
        ProposedChange(op='delete', path='experience.t.bullets.1', reason='Repeats the first bullet.'),
        ProposedChange(op='delete', path='experience.t.bullets.2', reason='Not relevant to this job.'),
        ProposedChange(op='delete', path='experience.t.bullets.3', reason='Not relevant to this job.'),
    ], TEACHER_CV)
    assert [s['original'] for s in d['suggestions']] == ['Made reports in Excel every week for the sales team']
    assert d['proposalStats']['rejected'] == {'delete_not_duplicate': 2}


def test_self_praise_and_spelled_out_figures_are_refused_and_outside_words_are_listed(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [
        rewrite('summary', 'Project Coordinator with over nine years of experience organising events.'),
        rewrite('experience.t.bullets.0', 'Prepared thorough weekly sales reports in Excel, ensuring accuracy'),
        rewrite('experience.t.bullets.2', 'Organised the annual school trip to London for 40 students, seeking to grow'),
        rewrite('experience.t.bullets.3', 'Coordinated weekly meetings of the language department'),
    ], TEACHER_CV)
    assert d['proposalStats']['rejected'] == {'adds_number': 1, 'adds_unsupported_claim': 2}
    (kept,) = d['suggestions']
    assert kept['suggested'].startswith('Coordinated') and kept['newTerms'] == ['coordinated']


def test_a_title_the_person_never_held_is_surfaced_word_by_word_for_review(client, monkeypatch):
    d = _tailor_with(client, monkeypatch, [
        rewrite('summary', 'Project Coordinator with six years of experience organising school trips and meetings.')], TEACHER_CV)
    (s,) = d['suggestions']   # the backend cannot judge the claim, but it shows exactly what is not from the CV
    assert s['newTerms'] == ['project', 'coordinator'] and s['risk'] == 'review'


def test_tailoring_is_declined_when_the_cv_shows_nothing_the_job_asks_for(client, monkeypatch):
    from app.ai_matching.matching import MatchAnalysis, Requirement
    from app.services.ai_matching import AnalysisResult
    nothing = MatchAnalysis(enough_info=True, requirements=[
        Requirement(id=str(i), requirement=name, category='required', source_quote=name, status='not_shown', explanation='x')
        for i, name in enumerate(['Python', 'MongoDB'])])
    monkeypatch.setattr(ai_matching, 'analyze', lambda *a, **k: AnalysisResult(analysis=nothing, usage=Usage(1, 1), model='mock'))
    called = []
    monkeypatch.setattr(ai_tailoring, 'analyze', lambda *a, **k: called.append(1))
    h = signup(client)
    mid = client.post(f'{BASE}/matches', headers=h,
                      json={'cvId': make_cv(client, h), 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}).json()['id']
    r = client.post(f'{BASE}/tailor', headers=h, json={'matchId': mid})
    assert r.status_code == 422 and 'does not show any of the qualifications' in r.json()['detail'] and not called
