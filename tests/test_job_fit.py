"""Keyword skill fit between jobs and a CV (no AI, no network)."""
import httpx

from app.config import settings
from app.jobs import service
from app.jobs.models import JobPosting
from app.jobs.skills import cv_skills, find_skills, fit, job_skills

BASE = '/api/v1'


def signup(client, email='ana@example.com'):
    r = client.post(f'{BASE}/auth/register', json={'name': 'Ana', 'email': email, 'password': 'secret123'})
    assert r.status_code == 201, r.text
    return {'Authorization': f"Bearer {r.json()['accessToken']}"}


def job(external_id, title, level='entry', description='Requirements\n- Good mood'):
    return {'external_id': external_id, 'title': title, 'company': '', 'vendor': '', 'slug': '', 'location': 'Warsaw',
            'city': 'Warsaw', 'url': f'https://example.com/{external_id}', 'level': level, 'level_signal': 'title',
            'description': description, 'posted_at': None}


# --- pure functions ---------------------------------------------------------------------------

def test_find_skills_avoids_common_false_matches():
    assert find_skills('the rest of the team') == set()
    assert find_skills('RESTful services and a REST API') == {'REST APIs'}
    assert find_skills('excellent communication') == set()
    assert find_skills('JavaScript, not Java?') == {'JavaScript', 'Java'}
    assert find_skills('JavaScript only') == {'JavaScript'}
    assert find_skills('C/C++ on Linux; digital tools') == {'C', 'C++', 'Linux'}
    assert find_skills('Postgres, k8s and scikit-learn') == {'PostgreSQL', 'Kubernetes', 'scikit-learn'}


AD = '\n'.join([
    'About us',
    'We build Elasticsearch and LLM products.',
    'What are we looking for?',
    '- Python and SQL in daily work',
    '- Docker',
    '- Fluent German and English (B2)',
    'Nice to have',
    '- Kubernetes',
    'What we offer',
    '- English lessons and an Excel course',
])


def test_job_skills_splits_required_optional_and_ignores_intro_and_benefits():
    s = job_skills('Junior Data Engineer', AD)
    assert s['required'] == ['Docker', 'Python', 'SQL']
    assert s['optional'] == ['Kubernetes']
    assert 'Elasticsearch' not in s['required'] and 'Excel' not in s['required']  # intro and benefits ignored
    assert s['languages'] == ['English', 'German']
    assert job_skills('Anything', None) is None


def test_job_skills_falls_back_to_whole_ad_when_sections_are_unknown():
    s = job_skills('Developer', 'About us\nWe like games.\nStuff we need\n- React and TypeScript\n- Jest tests and Git')
    assert {'React', 'TypeScript', 'Jest', 'Git'} <= set(s['required'])


def test_fit_score_label_and_missing_skills():
    cv = cv_skills({'skills': ['Python', 'SQL'], 'languages': 'English (C1)',
                    'experience': [{'title': 'Intern', 'bullets': ['Built Docker images']}]})
    assert cv == {'skills': {'Python', 'SQL', 'Docker'}, 'languages': {'English'}}
    job_s = {'required': ['Docker', 'Python', 'SQL', 'AWS'], 'optional': ['Kubernetes', 'Terraform'], 'languages': ['English']}
    f = fit(job_s, cv)
    assert f['score'] == 60 and f['label'] == 'Strong fit'  # 3 of (4 required + 2 optional at half weight)
    assert f['missing'] == ['AWS'] and f['missingOptional'] == ['Kubernetes', 'Terraform']
    # A required spoken language the CV does not have caps the score.
    capped = fit({**job_s, 'languages': ['German']}, cv)
    assert capped['score'] == 30 and capped['missingLanguages'] == ['German']
    # Too few known skills: no score instead of a misleading one.
    assert fit({'required': ['Python', 'SQL'], 'optional': [], 'languages': []}, cv)['score'] is None
    assert fit(None, cv)['label'] == 'No description'


# --- refresh --------------------------------------------------------------------------------------

def test_refresh_stores_skills_and_loads_missing_descriptions_once(client, monkeypatch):
    monkeypatch.setattr(settings, 'jobs_live_fetch', True)
    calls = []

    async def describe(http, vendor, slug, external_id):
        calls.append(external_id)
        return 'Requirements\n- Python, SQL and Docker'

    async def fetch(http, source):
        return [{**job('9', 'Junior Analyst', description=None),
                 'company': source['company'], 'vendor': source['vendor'], 'slug': source['slug']}]

    company = {'company': 'Bosch', 'vendor': 'smartrecruiters', 'slug': 'BoschGroup'}

    async def run():
        async with httpx.AsyncClient() as http:
            for _ in range(2):
                await service.refresh_company(http, company, {'smartrecruiters': fetch}, describe=describe)
    client.portal.call(run)
    posting = client.portal.call(lambda: JobPosting.find_one(JobPosting.key == 'smartrecruiters:BoschGroup:9'))
    assert posting.description.startswith('Requirements')
    assert posting.skills['required'] == ['Docker', 'Python', 'SQL']
    assert calls == ['9']  # loaded once, kept on the next refresh


def test_refresh_only_analyses_new_or_changed_jobs(client, monkeypatch):
    analysed = []
    real = service.job_skills

    def counting(title, description):
        analysed.append(title)
        return real(title, description)
    monkeypatch.setattr(service, 'job_skills', counting)
    company = {'company': 'Acme', 'vendor': 'greenhouse', 'slug': 'acme'}
    board = [job('1', 'Junior Dev', description='Requirements\n- Python'), job('2', 'Intern', description='Requirements\n- SQL')]

    async def fetch(http, source):
        return [{**j, 'company': source['company'], 'vendor': source['vendor'], 'slug': source['slug']} for j in board]

    async def run():
        async with httpx.AsyncClient() as http:
            await service.refresh_company(http, company, {'greenhouse': fetch})
    client.portal.call(run)
    assert sorted(analysed) == ['Intern', 'Junior Dev']
    analysed.clear()
    client.portal.call(run)  # nothing changed
    assert analysed == []
    board[1] = job('2', 'Intern', description='Requirements\n- SQL and Excel')  # description edited
    client.portal.call(run)
    assert analysed == ['Intern']


# --- API ---------------------------------------------------------------------------------------------

def make_cv(client, headers, skills):
    r = client.post(f'{BASE}/cvs', headers=headers, json={
        'name': 'My CV', 'kind': 'original',
        'content': {'fullName': 'Ana', 'skills': skills, 'languages': 'English (C1)', 'experience': [], 'projects': []}})
    assert r.status_code == 201, r.text
    return r.json()['id']


def seed_with_skills(client):
    companies = [{'company': 'Acme', 'vendor': 'greenhouse', 'slug': 'acme'},
                 {'company': 'Beta', 'vendor': 'lever', 'slug': 'beta'}]
    boards = {
        'acme': [job('1', 'Junior Python Developer', description='Requirements\n- Python, SQL, Docker'),
                 job('2', 'Junior Frontend Developer', description='Requirements\n- React, TypeScript, CSS'),
                 job('3', 'Junior Analyst')],
        'beta': [job('7', 'Data Intern', 'internship', 'Requirements\n- Python, SQL, Excel, Tableau')],
    }

    async def fetch(http, source):
        return [{**j, 'company': source['company'], 'vendor': source['vendor'], 'slug': source['slug']}
                for j in boards[source['slug']]]
    client.portal.call(lambda: service.refresh_all(companies, {'greenhouse': fetch, 'lever': fetch}))


def test_list_with_cv_adds_fit_and_sorts_best_first(client):
    h = signup(client)
    seed_with_skills(client)
    cv_id = make_cv(client, h, ['Python', 'SQL', 'Docker'])
    body = client.get(f'{BASE}/jobs', headers=h, params={'cvId': cv_id, 'sort': 'fit'}).json()
    assert [j['title'] for j in body['items']] == [
        'Junior Python Developer', 'Data Intern', 'Junior Frontend Developer', 'Junior Analyst']
    first = body['items'][0]['fit']
    assert first['score'] == 100 and first['label'] == 'Strong fit' and first['matched'] == ['Docker', 'Python', 'SQL']
    assert body['items'][1]['fit']['missing'] == ['Excel', 'Tableau']
    assert body['items'][3]['fit'] == {'score': None, 'label': 'Not enough detail', 'matched': [], 'missing': [],
                                       'missingOptional': [], 'missingLanguages': []}
    # Paging works on the ranked list.
    page2 = client.get(f'{BASE}/jobs', headers=h, params={'cvId': cv_id, 'sort': 'fit', 'pageSize': 2, 'page': 2}).json()
    assert [j['title'] for j in page2['items']] == ['Junior Frontend Developer', 'Junior Analyst']
    # Newest-first sorting also includes the fit when a CV is chosen; without a CV there is no fit.
    assert all('fit' in j for j in client.get(f'{BASE}/jobs', headers=h, params={'cvId': cv_id}).json()['items'])
    assert all('fit' not in j for j in client.get(f'{BASE}/jobs', headers=h).json()['items'])


def test_fit_needs_your_own_cv(client):
    ana = signup(client, 'ana@example.com')
    ana_cv = make_cv(client, ana, ['Python'])
    bob = signup(client, 'bob@example.com')
    assert client.get(f'{BASE}/jobs', headers=bob, params={'cvId': ana_cv}).status_code == 404
    assert client.get(f'{BASE}/jobs', headers=bob, params={'cvId': 'nope'}).status_code == 404
    assert client.get(f'{BASE}/jobs', headers=bob, params={'sort': 'fit'}).status_code == 422
