"""Smart CV Import into the main Profile: once per account, atomic, never stale, never touching saved CVs.

Runs against the disposable test MongoDB, so the unique index and single-document atomicity are the real ones.
OpenAI is never called (mock AI; tests that need a specific answer replace ai_cv_import.analyze).
"""
import asyncio

import pytest
from fastapi import HTTPException
from weasyprint import HTML

from app.cv_import.profile import ImportedProfileSave, save_imported_profile
from app.profiles.models import Profile
from app.services import ai_cv_import, ai_provider
from app.users.models import User

BASE = '/api/v1'
CONSENT = {'X-FlowJob-AI-Consent': 'cv-import', 'Content-Type': 'application/pdf'}

IMPORTED = {
    'fullName': 'Ada Example', 'email': 'ada.example@example.com', 'phone': '+48 600 123 456',
    'location': 'Wrocław, Poland', 'links': ['linkedin.com/in/ada-example'],
    'summary': 'Backend developer who builds reliable APIs.',
    'experience': [{'id': 'import-experience-1', 'title': 'Software Engineer', 'company': 'Acme Ltd',
                    'location': '', 'start': '2021-01', 'end': '', 'bullets': ['Built REST APIs']}],
    'education': [{'id': 'import-education-1', 'school': 'Wrocław University of Science and Technology',
                   'degree': 'BSc Computer Science', 'start': '2015', 'end': '2019', 'note': ''}],
    'projects': [{'id': 'import-project-1', 'name': 'FlowJob', 'tech': 'FastAPI, MongoDB', 'description': ''}],
    'skills': ['Python', 'FastAPI', 'MongoDB', 'Docker'],
    'languages': 'English (C1), Polish (native)',
}


def signup(client, email='ana@example.com', name='Ana Nowak'):
    r = client.post(f'{BASE}/auth/register', json={'name': name, 'email': email, 'password': 'secret123'})
    assert r.status_code == 201, r.text
    return {'Authorization': f"Bearer {r.json()['accessToken']}"}


def status(client, h):
    r = client.get(f'{BASE}/profile/import', headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def save(client, h, profile=None, revision=None, request_id='save-0001'):
    if revision is None:
        revision = status(client, h)['revision']
    return client.post(f'{BASE}/profile/import', headers=h, json={
        'profile': IMPORTED if profile is None else profile, 'revision': revision, 'requestId': request_id})


def marker(client, email='ana@example.com'):
    async def read():
        user = await User.find_one(User.email == email)
        doc = await Profile.get_pymongo_collection().find_one({'user_id': user.id})
        return doc and doc.get('cv_import')
    return client.portal.call(read)


def saved_cv_count(client):
    async def count():
        from app.integration.models import SavedCv
        return await SavedCv.get_pymongo_collection().count_documents({})
    return client.portal.call(count)


def pdf():
    return HTML(string='<h1>Ada Example</h1><p>ada.example@example.com</p><h2>Skills</h2><p>Python</p>').write_pdf()


# --- availability and the save -----------------------------------------------------------------------------------

def test_a_new_account_can_import_and_reviews_its_current_profile(client):
    h = signup(client)
    s = status(client, h)
    assert s['available'] is True and s['importedAt'] is None
    assert s['profile'] == client.get(f'{BASE}/profile', headers=h).json()
    assert s['profile']['fullName'] == 'Ana Nowak' and len(s['revision']) == 64
    assert client.get(f'{BASE}/profile/import').status_code == 401


def test_saving_writes_the_profile_once_and_creates_no_cv(client):
    h = signup(client)
    r = save(client, h)
    assert r.status_code == 200, r.text
    profile = client.get(f'{BASE}/profile', headers=h).json()
    assert r.json() == profile
    assert profile['experience'] == IMPORTED['experience'] and profile['skills'] == IMPORTED['skills']
    assert profile['languages'] == IMPORTED['languages']
    s = status(client, h)
    assert s['available'] is False and s['importedAt']
    assert client.get(f'{BASE}/cvs', headers=h).json() == [] and saved_cv_count(client) == 0
    # The legacy native profile is mirrored as PUT /profile does.
    native = client.get('/profile', headers=h).json()
    assert native['experience'][0]['position'] == 'Software Engineer'
    assert 'cv_import' not in native and 'cv_import' not in profile


def test_a_second_import_is_refused_and_changes_nothing(client):
    h = signup(client)
    assert save(client, h).status_code == 200
    before = client.get(f'{BASE}/profile', headers=h).json()
    r = save(client, h, {**IMPORTED, 'skills': ['Cobol']}, request_id='save-0002')
    assert r.status_code == 409 and 'already been imported' in r.json()['detail']
    assert client.get(f'{BASE}/profile', headers=h).json() == before


def test_a_retried_save_gets_the_same_answer(client):
    h = signup(client)
    revision = status(client, h)['revision']
    first = save(client, h, revision=revision)
    # The browser never saw the first answer and sends the same save again (same request id, old revision).
    again = save(client, h, revision=revision)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() == client.get(f'{BASE}/profile', headers=h).json()
    assert marker(client)['request_id'] == 'save-0001'


@pytest.mark.parametrize('existing_profile', [False, True])
def test_concurrent_saves_import_exactly_once(client, existing_profile):
    h = signup(client)
    if existing_profile:
        body = client.get(f'{BASE}/profile', headers=h).json()
        assert client.put(f'{BASE}/profile', headers=h, json={**body, 'summary': 'Mine'}).status_code == 200
    revision = status(client, h)['revision']

    async def race():
        user = await User.find_one(User.email == 'ana@example.com')

        async def attempt(request_id, skills):
            data = ImportedProfileSave.model_validate(
                {'profile': {**IMPORTED, 'skills': skills}, 'revision': revision, 'requestId': request_id})
            try:
                return await save_imported_profile(user, data)
            except HTTPException as error:
                return error.status_code
        return await asyncio.gather(*(attempt(f'race-{n:04}', [f'Skill {n}']) for n in range(6)))

    results = client.portal.call(race)
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1 and sorted(r for r in results if isinstance(r, int)) == [409] * 5
    assert client.get(f'{BASE}/profile', headers=h).json() == winners[0]
    assert marker(client)['request_id'] == f'race-{results.index(winners[0]):04}'


# --- stale reviews, conflicts and identity -------------------------------------------------------------------------

def test_a_review_of_an_outdated_profile_never_overwrites_it(client):
    h = signup(client)
    revision = status(client, h)['revision']
    body = client.get(f'{BASE}/profile', headers=h).json()
    # Edited in another tab while the review was open.
    assert client.put(f'{BASE}/profile', headers=h, json={**body, 'summary': 'Written by hand'}).status_code == 200
    r = save(client, h, revision=revision)
    assert r.status_code == 412 and 'changed' in r.json()['detail']
    assert client.get(f'{BASE}/profile', headers=h).json()['summary'] == 'Written by hand'
    assert status(client, h)['available'] is True  # nothing was used up
    assert save(client, h).status_code == 200  # a review of the current Profile saves


def test_an_account_edit_during_the_review_also_counts_as_a_change(client):
    h = signup(client)
    revision = status(client, h)['revision']
    assert client.patch(f'{BASE}/me', headers=h, json={'name': 'Ana Maria Nowak'}).status_code == 200
    assert save(client, h, revision=revision).status_code == 412


def test_the_reviewed_profile_is_saved_as_confirmed_including_kept_values(client):
    h = signup(client)
    body = client.get(f'{BASE}/profile', headers=h).json()
    mine = {**body, 'summary': 'My own words.', 'skills': ['Go'],
            'experience': [{'id': 'mine-1', 'title': 'Intern', 'company': 'Zeta', 'location': '',
                            'start': '2020-06', 'end': '2020-09', 'bullets': []}]}
    assert client.put(f'{BASE}/profile', headers=h, json=mine).status_code == 200
    # The review kept the Profile's summary and experience and chose the CV's skills and education.
    reviewed = {**IMPORTED, 'summary': mine['summary'], 'experience': mine['experience']}
    assert save(client, h, reviewed).status_code == 200
    profile = client.get(f'{BASE}/profile', headers=h).json()
    assert profile['summary'] == 'My own words.' and profile['experience'] == mine['experience']
    assert profile['skills'] == IMPORTED['skills'] and profile['education'] == IMPORTED['education']


def test_the_account_name_and_email_win_over_the_pdf(client):
    h = signup(client)
    assert save(client, h).status_code == 200
    profile = client.get(f'{BASE}/profile', headers=h).json()
    assert (profile['fullName'], profile['email']) == ('Ana Nowak', 'ana@example.com')


def test_a_name_set_in_the_profile_wins_too(client):
    h = signup(client, 'bo@example.com', 'Bo')
    body = client.get(f'{BASE}/profile', headers=h).json()
    assert client.put(f'{BASE}/profile', headers=h, json={**body, 'fullName': 'Bo Lindqvist'}).status_code == 200
    assert save(client, h).status_code == 200
    assert client.get(f'{BASE}/profile', headers=h).json()['fullName'] == 'Bo Lindqvist'


def test_a_profile_saved_only_in_the_legacy_format_can_import(client):
    h = signup(client)
    assert client.patch('/profile', headers=h, json={'skills': ['Rust']}).status_code == 200
    s = status(client, h)
    assert s['profile']['skills'] == ['Rust']
    assert save(client, h, revision=s['revision']).status_code == 200
    assert client.get(f'{BASE}/profile', headers=h).json()['skills'] == IMPORTED['skills']


@pytest.mark.parametrize('change, words', [
    ({'links': ['javascript:alert(1)']}, 'unsafe'),
    ({'experience': [IMPORTED['experience'][0]] * 101}, None),
    ({'surprise': 'x'}, None),
    ({'summary': 'bad\x00text'}, None),
])
def test_invalid_reviews_are_rejected_and_use_nothing_up(client, change, words):
    h = signup(client)
    r = save(client, h, {**IMPORTED, **change})
    assert r.status_code == 422
    if words:
        assert words in r.json()['detail']
    assert status(client, h)['available'] is True and marker(client) is None


@pytest.mark.parametrize('field, value', [('revision', 'abc'), ('requestId', 'no spaces allowed')])
def test_malformed_save_requests_are_rejected(client, field, value):
    h = signup(client)
    body = {'profile': IMPORTED, 'revision': status(client, h)['revision'], 'requestId': 'save-0001', field: value}
    assert client.post(f'{BASE}/profile/import', headers=h, json=body).status_code == 422
    assert client.post(f'{BASE}/profile/import', json=body).status_code == 401


# --- the marker is protected ---------------------------------------------------------------------------------------

def test_profile_saves_and_account_edits_never_erase_the_marker(client):
    h = signup(client)
    assert save(client, h).status_code == 200
    first = marker(client)
    body = client.get(f'{BASE}/profile', headers=h).json()
    for _ in range(2):  # edits stay unlimited after the import
        assert client.put(f'{BASE}/profile', headers=h, json={**body, 'summary': 'Edited'}).status_code == 200
    assert client.patch('/profile', headers=h, json={'skills': ['Go']}).status_code == 200
    assert client.patch(f'{BASE}/me', headers=h, json={'name': 'Ana Maria'}).status_code == 200
    assert client.patch('/users/me', headers=h, json={'full_name': 'Ana M.'}).status_code == 200
    assert marker(client) == first and status(client, h)['available'] is False
    assert client.get(f'{BASE}/profile', headers=h).json()['summary'] == 'Edited'


def test_an_outdated_profile_object_saved_after_the_import_keeps_the_marker(client):
    """Whole-document saves (every other profile write) cannot write the field, even from a copy loaded before."""
    h = signup(client)
    body = client.get(f'{BASE}/profile', headers=h).json()
    assert client.put(f'{BASE}/profile', headers=h, json=body).status_code == 200

    async def load():
        user = await User.find_one(User.email == 'ana@example.com')
        return await Profile.find_one(Profile.user_id == user.id)
    stale = client.portal.call(load)
    assert stale.cv_import is None
    assert save(client, h).status_code == 200
    assert client.portal.call(load).cv_import.request_id == 'save-0001'  # modeled and readable

    async def write_stale():
        stale.frontend_data = {**stale.frontend_data, 'summary': 'stale'}
        await stale.save()
    client.portal.call(write_stale)
    assert marker(client)['request_id'] == 'save-0001'


# --- extraction and the one-time import ----------------------------------------------------------------------------

def extract(client, h, headers=CONSENT):
    return client.post(f'{BASE}/profile/import/pdf', content=pdf(), headers={**h, **headers})


def test_extraction_is_refused_once_imported_without_ai_or_quota(client, monkeypatch):
    from tests.test_cv_import import quota_used
    h = signup(client)
    assert save(client, h).status_code == 200

    def never(*args):
        raise AssertionError('the AI must not be called')
    monkeypatch.setattr(ai_cv_import, 'analyze', never)
    for headers in (CONSENT, {'Content-Type': 'application/pdf'}):  # checked before consent and the file
        r = extract(client, h, headers)
        assert r.status_code == 409 and 'already been imported' in r.json()['detail']
    assert quota_used(client) == (0, 0)


def test_failed_or_discarded_extractions_never_use_up_the_import(client, monkeypatch):
    h = signup(client)
    assert client.post(f'{BASE}/profile/import/pdf', content=b'not a pdf', headers={**h, **CONSENT}).status_code == 415
    assert extract(client, h, {'Content-Type': 'application/pdf'}).status_code == 428

    def boom(*args):
        raise ai_provider.AiTimeout()
    monkeypatch.setattr(ai_cv_import, 'analyze', boom)
    assert extract(client, h).status_code == 504
    monkeypatch.undo()
    assert extract(client, h).status_code == 200  # reviewed, then discarded: nothing is saved
    assert status(client, h)['available'] is True and marker(client) is None
    assert save(client, h).status_code == 200


def test_one_account_s_import_does_not_affect_another(client):
    ana = signup(client)
    bo = signup(client, 'bo@example.com', 'Bo')
    assert save(client, ana).status_code == 200
    assert status(client, bo)['available'] is True
    assert extract(client, bo).status_code == 200
    assert save(client, bo).status_code == 200


# --- saved CVs stay independent -----------------------------------------------------------------------------------

def test_cvs_are_independent_copies_of_the_profile(client):
    h = signup(client)
    before = client.post(f'{BASE}/cvs', headers=h, json={'name': 'Older CV', 'kind': 'original'}).json()
    assert save(client, h).status_code == 200
    assert client.get(f'{BASE}/cvs/{before["id"]}', headers=h).json()['content'] == before['content']

    made = client.post(f'{BASE}/cvs', headers=h, json={'name': 'Original CV', 'kind': 'original'})
    assert made.status_code == 201
    copy = made.json()['content']
    assert copy['experience'] == IMPORTED['experience'] and copy['fullName'] == 'Ana Nowak'
    tailored = client.post(f'{BASE}/cvs', headers=h, json={
        'name': 'For Acme', 'kind': 'tailored', 'sourceCvId': made.json()['id'], 'content': copy}).json()

    body = client.get(f'{BASE}/profile', headers=h).json()
    assert client.put(f'{BASE}/profile', headers=h, json={**body, 'skills': ['Changed'], 'experience': []}).status_code == 200
    assert client.get(f'{BASE}/cvs/{made.json()["id"]}', headers=h).json()['content'] == copy
    assert client.get(f'{BASE}/cvs/{tailored["id"]}', headers=h).json()['content'] == copy
    assert client.get(f'{BASE}/cvs/{before["id"]}', headers=h).json()['content'] == before['content']
