"""Job listings: classification, fetchers (fake HTTP), cache refresh and the /api/v1/jobs API.

No test touches the network: fetchers get an httpx MockTransport and the refresh uses fake fetchers.
"""
import asyncio

import httpx

from app.jobs import service
from app.jobs.classify import classify_level, html_to_text, polish_city
from app.jobs.fetchers import FETCHERS, FetchError
from app.jobs.models import JobPosting, JobSource

BASE = '/api/v1'


def signup(client, email='ana@example.com'):
    r = client.post(f'{BASE}/auth/register', json={'name': 'Ana', 'email': email, 'password': 'secret123'})
    assert r.status_code == 201, r.text
    return {'Authorization': f"Bearer {r.json()['accessToken']}"}


# --- classification (pure functions) ---------------------------------------------------

def test_polish_city_recognises_spellings_and_country():
    assert polish_city('Wroclaw, Lower Silesia') == 'Wrocław'
    assert polish_city('Warszawa | Hybrid') == 'Warsaw'
    assert polish_city('Cracow') == 'Kraków'
    assert polish_city('Remote - Poland') == 'Poland'
    assert polish_city('Berlin, Germany') is None
    assert polish_city('') is None


def test_level_from_title_structured_field_and_description():
    assert classify_level('Software Engineering Intern')[0] == 'internship'
    assert classify_level('Płatny Staż w Dziale Jakości')[0] == 'internship'
    assert classify_level('Junior Project Manager')[0] == 'entry'  # "Manager" does not block "Junior"
    assert classify_level('Senior Junior-Mentor Lead')[0] == 'other'  # explicit seniority wins
    assert classify_level('Backend Developer', structured='internship') == ('internship', 'api')
    assert classify_level('Data Analyst', description='Wymagamy statusu studenta... status studenta') == ('entry', 'description')
    # The weak description signal is ignored for roles that usually need experience.
    assert classify_level('Product Manager', description='status studenta')[0] == 'other'
    assert classify_level('Backend Developer')[0] == 'other'


def test_html_to_text_keeps_structure():
    text = html_to_text('&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;')
    assert 'Hello & welcome' in text and '- Python' in text and '<' not in text


# --- fetchers with a fake HTTP transport -------------------------------------------------

def run_fetcher(vendor, payload, status=200, slug='acme'):
    def handler(request):
        return httpx.Response(status, json=payload)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await FETCHERS[vendor](client, {'company': 'Acme', 'vendor': vendor, 'slug': slug})
    return asyncio.run(go())


def test_greenhouse_keeps_only_polish_jobs_with_description():
    jobs = run_fetcher('greenhouse', {'jobs': [
        {'id': 1, 'title': 'Junior Developer', 'location': {'name': 'Kraków, Poland'}, 'absolute_url': 'https://x/1',
         'first_published': '2026-11-01T10:00:00Z', 'content': '&lt;p&gt;Build things&lt;/p&gt;', 'offices': []},
        {'id': 2, 'title': 'Engineer', 'location': {'name': 'Dublin, Ireland'}, 'offices': []},
    ]})
    assert len(jobs) == 1
    j = jobs[0]
    assert (j['title'], j['city'], j['level'], j['external_id']) == ('Junior Developer', 'Kraków', 'entry', '1')
    assert j['description'] == 'Build things' and j['posted_at'].year == 2026


def test_lever_internship_commitment_and_country_code():
    jobs = run_fetcher('lever', [
        {'id': 'a', 'text': 'Data Team', 'country': 'PL', 'categories': {'commitment': 'Internship', 'location': 'Remote'},
         'hostedUrl': 'https://jobs.lever.co/acme/a', 'createdAt': 1767225600000, 'descriptionPlain': 'Learn data'},
    ])
    assert jobs[0]['level'] == 'internship' and jobs[0]['level_signal'] == 'api' and jobs[0]['city'] == 'Poland'


def test_smartrecruiters_uses_experience_level_and_has_no_description():
    jobs = run_fetcher('smartrecruiters', {'totalFound': 1, 'content': [
        {'id': '9', 'name': 'Automation Specialist', 'location': {'city': 'Łódź', 'country': 'pl'},
         'experienceLevel': {'id': 'internship'}, 'releasedDate': '2026-11-09T13:52:45.591Z'},
    ]}, slug='BoschGroup')
    assert jobs[0]['level'] == 'internship' and jobs[0]['city'] == 'Łódź' and jobs[0]['description'] is None
    assert jobs[0]['url'] == 'https://jobs.smartrecruiters.com/BoschGroup/9'


def test_fetcher_raises_on_http_error():
    try:
        run_fetcher('ashby', {}, status=404)
    except FetchError as e:
        assert '404' in str(e)
    else:
        raise AssertionError('expected FetchError')


# --- refresh into MongoDB with fake fetchers ---------------------------------------------

COMPANIES = [{'company': 'Acme', 'vendor': 'greenhouse', 'slug': 'acme'},
             {'company': 'Beta', 'vendor': 'lever', 'slug': 'beta'}]


def job(external_id, title, city='Warsaw', level='entry', description='About the role'):
    return {'external_id': external_id, 'title': title, 'company': '', 'vendor': '', 'slug': '', 'location': city,
            'city': city, 'url': f'https://example.com/{external_id}', 'level': level, 'level_signal': 'title',
            'description': description, 'posted_at': None}


def fake_fetchers(boards):
    """boards: {slug: list of jobs | Exception}. Fills company/vendor/slug like the real fetchers do."""
    async def fetch(client, source):
        result = boards[source['slug']]
        if isinstance(result, Exception):
            raise result
        return [{**j, 'company': source['company'], 'vendor': source['vendor'], 'slug': source['slug']} for j in result]
    return {'greenhouse': fetch, 'lever': fetch}


def refresh(client, boards):
    return client.portal.call(lambda: service.refresh_all(COMPANIES, fake_fetchers(boards)))


def test_refresh_inserts_updates_and_removes_closed_jobs(client):
    summary = refresh(client, {'acme': [job('1', 'Junior Dev'), job('2', 'Intern', level='internship')],
                               'beta': [job('7', 'Senior Engineer', 'Kraków', 'other')]})
    assert summary == {'companies': 2, 'jobs': 3}
    assert client.portal.call(JobPosting.count) == 3

    # Job 2 closed, job 1 renamed, a new job 3 appeared.
    refresh(client, {'acme': [job('1', 'Junior Developer'), job('3', 'Graduate Analyst')],
                     'beta': [job('7', 'Senior Engineer', 'Kraków', 'other')]})
    titles = sorted(p.title for p in client.portal.call(JobPosting.find_all().to_list))
    assert titles == ['Graduate Analyst', 'Junior Developer', 'Senior Engineer']


def test_failed_source_keeps_previous_jobs_and_records_error(client):
    refresh(client, {'acme': [job('1', 'Junior Dev')], 'beta': [job('7', 'Intern', level='internship')]})
    refresh(client, {'acme': FetchError('HTTP 503'), 'beta': [job('7', 'Intern', level='internship')]})
    assert client.portal.call(JobPosting.count) == 2  # Acme's job is kept
    source = client.portal.call(lambda: JobSource.find_one(JobSource.key == 'greenhouse:acme'))
    assert source.last_ok is False and 'HTTP 503' in source.last_error


def test_description_loaded_on_demand_is_kept_on_refresh(client):
    refresh(client, {'acme': [job('1', 'Junior Dev', description='Full text')], 'beta': []})
    refresh(client, {'acme': [job('1', 'Junior Dev', description=None)], 'beta': []})
    posting = client.portal.call(lambda: JobPosting.find_one(JobPosting.key == 'greenhouse:acme:1'))
    assert posting.description == 'Full text'


# --- API ----------------------------------------------------------------------------------

def seed(client):
    refresh(client, {
        'acme': [job('1', 'Junior Developer', 'Warsaw'), job('2', 'Summer Intern', 'Wrocław', 'internship'),
                 job('3', 'Staff Engineer', 'Warsaw', 'other')],
        'beta': [job('7', 'Graduate Analyst', 'Kraków'), job('8', 'Data Intern', 'Wrocław', 'internship')],
    })


def test_jobs_require_authentication(client):
    assert client.get(f'{BASE}/jobs').status_code == 401
    assert client.get(f'{BASE}/jobs/abc').status_code == 401


def test_default_list_shows_entry_level_and_internships(client):
    h = signup(client)
    seed(client)
    r = client.get(f'{BASE}/jobs', headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['total'] == 4 and 'Staff Engineer' not in [j['title'] for j in body['items']]
    assert body['levels'] == {'entry': 2, 'internship': 2, 'other': 1}
    assert body['refreshing'] is False  # live fetching is off in tests
    item = body['items'][0]
    assert set(item) == {'id', 'title', 'company', 'city', 'location', 'level', 'url', 'postedAt', 'source'}
    assert 'description' not in item  # the list stays small


def test_filters_search_facets_and_paging(client):
    h = signup(client)
    seed(client)
    get = lambda **params: client.get(f'{BASE}/jobs', headers=h, params=params).json()
    assert {j['title'] for j in get(level='internship')['items']} == {'Summer Intern', 'Data Intern'}
    assert get(level='all')['total'] == 5
    assert {j['title'] for j in get(city='Wrocław')['items']} == {'Summer Intern', 'Data Intern'}
    assert {j['title'] for j in get(company='Beta')['items']} == {'Graduate Analyst', 'Data Intern'}
    assert [j['title'] for j in get(q='graduate')['items']] == ['Graduate Analyst']
    assert get(q='.*')['total'] == 0  # search text is not treated as a regex
    facets = get(city='Wrocław')
    assert {c['name'] for c in facets['cities']} == {'Warsaw', 'Wrocław', 'Kraków'}  # city filter keeps all cities visible
    assert {c['name']: c['count'] for c in facets['companies']} == {'Acme': 1, 'Beta': 1}
    page2 = get(pageSize=3, page=2)
    assert page2['total'] == 4 and len(page2['items']) == 1


def test_invalid_filter_values_are_rejected(client):
    h = signup(client)
    assert client.get(f'{BASE}/jobs', headers=h, params={'level': 'senior'}).status_code == 422
    assert client.get(f'{BASE}/jobs', headers=h, params={'pageSize': 500}).status_code == 422


def test_job_detail_includes_description(client):
    h = signup(client)
    seed(client)
    job_id = client.get(f'{BASE}/jobs', headers=h, params={'q': 'Junior'}).json()['items'][0]['id']
    r = client.get(f'{BASE}/jobs/{job_id}', headers=h)
    assert r.status_code == 200 and r.json()['description'] == 'About the role'
    assert client.get(f'{BASE}/jobs/not-an-id', headers=h).status_code == 404
    assert client.get(f'{BASE}/jobs/000000000000000000000000', headers=h).status_code == 404


def test_only_web_links_are_kept():
    from app.jobs.fetchers import _safe_url
    assert _safe_url('https://jobs.example/1') == 'https://jobs.example/1'
    assert _safe_url(' http://jobs.example/1 ') == 'http://jobs.example/1'
    assert _safe_url('javascript:alert(1)') is None
    assert _safe_url('data:text/html,hi') is None
    assert _safe_url(None) is None
