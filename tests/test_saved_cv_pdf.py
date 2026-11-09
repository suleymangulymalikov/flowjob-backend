"""Saved-CV PDF: renders the canonical Harvard template from a CV's own content.

Covers the two fixed bugs (preview/download now use the backend PDF) plus the data
mapping that previously dropped free-form dates and languages from the PDF.
"""
from weasyprint import HTML

from app.cv.render import build_cv_context, env, format_dates_loose, render_saved_cv_pdf

BASE = '/api/v1'

RICH_CONTENT = {
    'fullName': 'Jane Doe', 'email': 'jane@example.com', 'phone': '555-0100',
    'location': 'Boston, MA', 'links': ['https://linkedin.com/in/jane'],
    'summary': 'Full-stack engineer.',
    'experience': [
        {'id': '0', 'title': 'Senior Engineer', 'company': 'Acme', 'location': 'Remote',
         'start': '2021-03', 'end': '2024-06', 'bullets': ['Led team', 'Shipped X']},
        {'id': '1', 'title': 'Intern', 'company': 'Beta', 'location': 'NYC',
         'start': 'Summer 2019', 'end': '', 'bullets': ['Free-form date entry']},
    ],
    'education': [{'id': '0', 'school': 'MIT', 'degree': 'BSc CS',
                   'start': '2015-09', 'end': '2019-05', 'note': 'Honors'}],
    'projects': [{'id': '0', 'name': 'FlowJob', 'tech': 'Python, React', 'description': 'CV tool'}],
    'skills': ['Python', 'React', 'MongoDB'],
    'languages': 'English Native, Spanish B2',
}


# --- unit: date tolerance + field mapping ---------------------------------

def test_format_dates_loose_handles_yearmonth_and_freeform():
    assert format_dates_loose('2021-03', '2024-06') == 'Mar 2021 – Jun 2024'
    assert format_dates_loose('2021-03', '') == 'Mar 2021 – Present'
    assert format_dates_loose('Summer 2019', '') == 'Summer 2019 – Present'
    assert format_dates_loose('', '') == ''


def test_build_cv_context_maps_all_fields_without_dropping():
    ctx = build_cv_context('Jane Doe', 'jane@example.com', RICH_CONTENT)
    # free-form-dated experience is NOT dropped (both entries present)
    assert len(ctx['experience']) == 2
    assert ctx['experience'][0]['position'] == 'Senior Engineer'
    assert ctx['experience'][1]['dates'] == 'Summer 2019 – Present'
    # education mapping school->institution, note->description
    assert ctx['education'][0]['institution'] == 'MIT'
    assert ctx['education'][0]['description'] == 'Honors'
    # projects tech -> technologies list
    assert ctx['projects'][0]['technologies'] == ['Python', 'React']
    # languages included in the PDF skills lines (previously missing)
    assert ('Languages', 'English Native, Spanish B2') in ctx['skills_lines']
    assert ('Technical', 'Python, React, MongoDB') in ctx['skills_lines']


def test_render_saved_cv_pdf_produces_pdf():
    pdf = render_saved_cv_pdf('Jane Doe', 'jane@example.com', RICH_CONTENT)
    assert pdf[:5] == b'%PDF-' and len(pdf) > 1000


def test_long_cv_renders_multiple_pages_without_error():
    big = {**RICH_CONTENT, 'experience': RICH_CONTENT['experience'] * 40}
    html = env.get_template('harvard.html').render(
        **build_cv_context('Jane', 'jane@example.com', big), font_size=9, margin=0.4)
    assert len(HTML(string=html).render().pages) > 1
    pdf = render_saved_cv_pdf('Jane', 'jane@example.com', big)
    assert pdf[:5] == b'%PDF-'


# --- endpoint: auth, ownership, content type ------------------------------

def _signup(client, email='jane@example.com'):
    r = client.post(f'{BASE}/auth/register',
                    json={'name': 'Jane Doe', 'email': email, 'password': 'secret123'})
    assert r.status_code == 201, r.text
    return {'Authorization': f"Bearer {r.json()['accessToken']}"}


def _create_cv(client, headers, content):
    r = client.post(f'{BASE}/cvs', json={'name': 'My CV', 'kind': 'original', 'content': content},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()['id']


def test_saved_cv_pdf_requires_auth(client):
    headers = _signup(client)
    cv_id = _create_cv(client, headers, RICH_CONTENT)
    assert client.get(f'{BASE}/cvs/{cv_id}/pdf').status_code == 401


def test_saved_cv_pdf_returns_harvard_pdf_from_saved_content(client):
    headers = _signup(client)
    cv_id = _create_cv(client, headers, RICH_CONTENT)
    res = client.get(f'{BASE}/cvs/{cv_id}/pdf', headers=headers)
    assert res.status_code == 200, res.text
    assert res.headers['content-type'] == 'application/pdf'
    assert 'filename="My_CV.pdf"' in res.headers.get('content-disposition', '')
    assert res.content[:5] == b'%PDF-'


def test_saved_cv_pdf_user_isolation(client):
    jane = _signup(client)
    cv_id = _create_cv(client, jane, RICH_CONTENT)
    other = _signup(client, 'other@example.com')
    assert client.get(f'{BASE}/cvs/{cv_id}/pdf', headers=other).status_code == 404


def test_saved_cv_pdf_unknown_id_is_404(client):
    headers = _signup(client)
    assert client.get(f'{BASE}/cvs/not-an-id/pdf', headers=headers).status_code == 404


# --- live builder preview endpoint (stateless, no DB writes) ---------------

def test_preview_pdf_requires_auth(client):
    assert client.post(f'{BASE}/cvs/preview/pdf', json={'content': RICH_CONTENT}).status_code == 401


def test_preview_pdf_renders_without_saving(client):
    headers = _signup(client)
    res = client.post(f'{BASE}/cvs/preview/pdf', json={'content': RICH_CONTENT}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.headers['content-type'] == 'application/pdf'
    assert res.content[:5] == b'%PDF-'
    # No CV document was created by previewing.
    assert client.get(f'{BASE}/cvs', headers=headers).json() == []


def test_preview_pdf_handles_empty_and_multipage(client):
    headers = _signup(client)
    assert client.post(f'{BASE}/cvs/preview/pdf', json={'content': {}}, headers=headers).status_code == 200
    big = {**RICH_CONTENT, 'experience': RICH_CONTENT['experience'] * 40}
    res = client.post(f'{BASE}/cvs/preview/pdf', json={'content': big}, headers=headers)
    assert res.status_code == 200 and res.content[:5] == b'%PDF-'


# --- contact links: LinkedIn / GitHub shown as clickable labels --------------

def _contact_html(links):
    ctx = build_cv_context('Jane Doe', 'jane@example.com', {**RICH_CONTENT, 'links': links})
    return env.get_template('harvard.html').render(**ctx, font_size=11, margin=0.75)


def test_linkedin_and_github_render_as_labelled_links_not_raw_urls():
    html = _contact_html(['linkedin.com/in/jane', 'https://github.com/jane'])
    assert '>LinkedIn</a>' in html and '>GitHub</a>' in html
    assert 'href="https://linkedin.com/in/jane"' in html and 'href="https://github.com/jane"' in html
    assert '>linkedin.com/in/jane<' not in html and '>https://github.com/jane<' not in html
    assert 'color:inherit;text-decoration:none' in html  # keeps the Harvard look (no blue underline)


def test_unrelated_and_unsafe_legacy_links_are_kept_verbatim_and_never_linked():
    html = _contact_html(['https://janedoe.dev', 'javascript:alert(1)', 'https://evil.com/linkedin.com', 'portfolio'])
    for raw in ('https://janedoe.dev', 'javascript:alert(1)', 'https://evil.com/linkedin.com', 'portfolio'):
        assert raw in html
    assert '<a href' not in html


def test_link_values_are_html_escaped():
    html = _contact_html(['"><script>x</script>'])
    assert '<script>x</script>' not in html


def test_labelled_links_are_clickable_in_the_pdf_and_labels_replace_urls():
    html = _contact_html(['linkedin.com/in/jane', 'github.com/jane'])
    pdf = HTML(string=html).write_pdf(uncompressed_pdf=True)
    assert b'/URI (https://linkedin.com/in/jane)' in pdf and b'/URI (https://github.com/jane)' in pdf
    # The production render path produces a valid PDF with the same links.
    assert render_saved_cv_pdf('Jane Doe', 'jane@example.com', {**RICH_CONTENT, 'links': ['github.com/jane']})[:5] == b'%PDF-'
