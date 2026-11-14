"""Checkpoint 5: the complete Gmail workflow end to end through the HTTP API, with a fake Google (OAuth and
Gmail) and a fake model. This proves the integration of the pieces; it is NOT a live Google/OpenAI test."""
from datetime import UTC, datetime, timedelta

from app.gmail import sync
from app.gmail.classify import EmailAnalysis
from app.gmail.google import InvalidGrant
from app.gmail.models import GmailConnection, GmailMessage, GmailReview, GmailSuppression
from app.applications.models import ApplicationEvent
from app.services.ai_provider import AiTimeout
from tests.gmail_fakes import BASE, callback, fake_google, make_message, signup, start_flow  # noqa: F401
from tests.test_gmail_pipeline import FakeAI, ai, item, run  # noqa: F401


def test_complete_gmail_workflow(client, fake_google, ai):
    c = client
    now = datetime.now(UTC)
    inbox = fake_google.messages

    def mail(mid, subject, body, analysis, *, days=0.0, minutes=0, thread=None):
        inbox[mid] = make_message(mid, subject=subject, body=body, thread=thread,
                                  at=now - timedelta(days=days, minutes=minutes))
        ai.by_marker[subject] = EmailAnalysis(is_recruitment=True, items=analysis)

    # An inbox with a confirmation, an inferred Polish application, an advertisement, an uncertain recruiter
    # email, a multi-application email and an old interview email for an application tracked by hand.
    mail('a00000000000000a', 'Application received SUBJ-A', 'Thank you for applying for Junior Data Analyst at Acme.',
         [item(supporting_quote='Thank you for applying for Junior Data Analyst')], days=20)
    mail('b00000000000000b', 'Rekrutacja SUBJ-B',
         'Zapraszamy na rozmowę kwalifikacyjną na stanowisko Młodszy Programista (nr REQ-77) w Kowalski S.A.',
         [item(kind='submitted_application', event_type='interview_invitation', employer='Kowalski S.A.',
               job_title='Młodszy Programista', references=['REQ-77'], application_evidence='active_process',
               supporting_quote='Zapraszamy na rozmowę kwalifikacyjną na stanowisko Młodszy Programista',
               proposed_status='Interview', scheduled_at='2026-12-10T10:00')], days=15)
    mail('c00000000000000c', 'New jobs for you SUBJ-C', 'Junior jobs near you. Apply now!',
         [item(kind='newsletter', application_evidence='none', supporting_quote='Junior jobs near you')], days=10)
    mail('d00000000000000d', 'Your profile SUBJ-D', 'Hi, about the Data Engineer role at Beta, can we chat?',
         [item(employer='Beta', job_title='Data Engineer', application_evidence='none', confidence='medium',
               event_type='recruiter_follow_up', supporting_quote='about the Data Engineer role at Beta')], days=8)
    mail('e00000000000000e', 'Applications received SUBJ-E',
         'We received your applications for Backend Intern and Frontend Intern at Gamma.',
         [item(employer='Gamma', job_title='Backend Intern', supporting_quote='We received your applications'),
          item(employer='Gamma', job_title='Frontend Intern', supporting_quote='We received your applications')],
         days=6)

    # 1-3. Connect; the OAuth callback completes and starts the 30-day backfill.
    headers = signup(c, name='Ana Nowak')
    manual = c.post(f'{BASE}/applications', headers=headers,
                    json={'company': 'Delta', 'jobTitle': 'QA Intern', 'status': 'Applied'}).json()
    mail('f00000000000000f', 'Interview SUBJ-F', 'We would like to invite you to an interview for QA Intern at Delta.',
         [item(kind='existing_application_update', event_type='interview_invitation', employer='Delta',
               job_title='QA Intern', match_key='A1', application_evidence='active_process',
               supporting_quote='invite you to an interview for QA Intern', proposed_status='Interview')], days=1)
    params, cookie = start_flow(c, headers)
    assert callback(c, params['state'], cookie=cookie)['gmail'] == ['connected']

    # 4-11. Candidates were found and classified; strong applications imported without a CV; uncertain ones and
    # status changes wait for review; interview dates extracted.
    apps = {(a['company'], a['jobTitle']): a for a in c.get(f'{BASE}/applications', headers=headers).json()}
    assert set(apps) == {('Acme', 'Junior Data Analyst'), ('Kowalski S.A.', 'Młodszy Programista'),
                         ('Gamma', 'Backend Intern'), ('Gamma', 'Frontend Intern'), ('Delta', 'QA Intern')}
    assert all(a['cvId'] is None for a in apps.values())
    assert apps[('Kowalski S.A.', 'Młodszy Programista')]['activity'][0]['scheduledAt'] == '2026-12-10T09:00:00+00:00'
    status = c.get(f'{BASE}/gmail', headers=headers).json()
    assert status['sync']['caughtUp'] and status['sync']['lastRun']['imported'] == 4
    pending = c.get(f'{BASE}/gmail/reviews', headers=headers).json()
    kinds = sorted((r['kind'], (r['proposal'].get('company') or r['application']['company'])) for r in pending)
    assert kinds == [('new_application', 'Beta'), ('status_change', 'Delta'), ('status_change', 'Kowalski S.A.')]
    assert status['pendingReviewCount'] == 3
    delta = apps[('Delta', 'QA Intern')]
    assert delta['status'] == 'Applied' and delta['suggestion']['proposedStatus'] == 'Interview'

    # 10. Status suggestions require approval.
    assert c.post(f'{BASE}/gmail/reviews/{delta["suggestion"]["id"]}/approve', headers=headers, json={}).status_code == 200
    assert c.get(f'{BASE}/applications/{manual["id"]}', headers=headers).json()['status'] == 'Interview'

    # 12. A clear reschedule updates the displayed schedule and keeps the previous date.
    kowalski = apps[('Kowalski S.A.', 'Młodszy Programista')]
    mail('a10000000000000a', 'Zmiana terminu SUBJ-G', 'Przenosimy rozmowę (REQ-77) na 2026-12-12 o 14:00.',
         [item(kind='existing_application_update', event_type='interview_rescheduled', employer='Kowalski S.A.',
               job_title='Młodszy Programista', references=['REQ-77'], application_evidence='active_process',
               supporting_quote='Przenosimy rozmowę (REQ-77) na 2026-12-12', scheduled_at='2026-12-12T14:00',
               schedule_change='clear')], minutes=30)
    conn = run(c, GmailConnection.find_one())
    run(c, sync.run_now(conn.id, trigger='manual'))
    timeline = c.get(f'{BASE}/applications/{kowalski["id"]}', headers=headers).json()['activity']
    assert timeline[-1]['scheduledAt'] == '2026-12-12T13:00:00+00:00'
    assert timeline[-1]['previousScheduledAt'] == '2026-12-10T09:00:00+00:00'

    # 13. A manual decision blocks suggestions from older emails.
    c.patch(f'{BASE}/applications/{kowalski["id"]}', headers=headers, json={'status': 'Withdrawn'})
    assert c.get(f'{BASE}/applications/{kowalski["id"]}', headers=headers).json()['suggestion'] is None
    assert not [r for r in c.get(f'{BASE}/gmail/reviews', headers=headers).json() if r['kind'] == 'status_change']

    # 14. Incorrect events can be corrected or dismissed.
    acme = apps[('Acme', 'Junior Data Analyst')]
    event_id = acme['activity'][0]['id']
    r = c.patch(f'{BASE}/applications/{acme["id"]}/activity/{event_id}', headers=headers,
                json={'eventType': 'application_update'})
    assert r.json()['activity'][0]['corrected'] is True

    # 15. A deleted import cannot be recreated by the same evidence.
    gamma = apps[('Gamma', 'Frontend Intern')]
    assert c.delete(f'{BASE}/applications/{gamma["id"]}', headers=headers).status_code == 204
    run(c, GmailMessage.get_pymongo_collection().update_one(
        {'message_id': 'e00000000000000e'}, {'$set': {'state': 'failed', 'analysis': None}}))
    run(c, GmailConnection.get_pymongo_collection().update_one({}, {'$set': {'last_manual_sync_at': None}}))
    assert c.post(f'{BASE}/gmail/sync', headers=headers, json={'retryFailed': True}).status_code == 202
    titles = sorted(a['jobTitle'] for a in c.get(f'{BASE}/applications', headers=headers).json())
    assert 'Frontend Intern' not in titles and titles.count('Backend Intern') == 1

    # 16-17. Progress survives restarts and partial failures; failed work needs a manual retry.
    ai.error = AiTimeout()
    mail('a20000000000000a', 'Application received SUBJ-H', 'Thank you for applying for Analyst at Epsilon.',
         [item(employer='Epsilon', job_title='Analyst', supporting_quote='Thank you for applying for Analyst')],
         minutes=20)
    run(c, sync.run_now(conn.id, trigger='scheduled'))
    assert c.get(f'{BASE}/gmail', headers=headers).json()['sync']['failed'] == 1
    ai.error = None
    run(c, sync.run_now(conn.id, trigger='scheduled'))
    assert 'Epsilon' not in {a['company'] for a in c.get(f'{BASE}/applications', headers=headers).json()}
    run(c, GmailConnection.get_pymongo_collection().update_one({}, {'$set': {'last_manual_sync_at': None}}))
    c.post(f'{BASE}/gmail/sync', headers=headers, json={'retryFailed': True})
    assert 'Epsilon' in {a['company'] for a in c.get(f'{BASE}/applications', headers=headers).json()}

    # 18-19. Expired authorization requires Reconnect; reconnecting keeps progress and pending reviews.
    fake_google.refresh_error = InvalidGrant()
    run(c, sync.run_now(conn.id, trigger='scheduled'))
    assert c.get(f'{BASE}/gmail', headers=headers).json()['connection']['state'] == 'reauth_required'
    before = run(c, GmailMessage.find_all().count()), run(c, GmailReview.find(GmailReview.state == 'pending').count())
    fake_google.refresh_error = None
    params, cookie = start_flow(c, headers, mode='reconnect')
    assert callback(c, params['state'], cookie=cookie)['gmail'] == ['reconnected']
    after = run(c, GmailMessage.find_all().count()), run(c, GmailReview.find(GmailReview.state == 'pending').count())
    assert after == before and c.get(f'{BASE}/gmail', headers=headers).json()['connection']['state'] == 'active'

    # 20. Disconnect erases Gmail-derived data but keeps the applications and approved statuses.
    count_apps = len(c.get(f'{BASE}/applications', headers=headers).json())
    assert c.delete(f'{BASE}/gmail', headers=headers).status_code == 204
    left = c.get(f'{BASE}/applications', headers=headers).json()
    assert len(left) == count_apps
    assert all(a['activity'] == [] and a['suggestion'] is None for a in left)
    assert c.get(f'{BASE}/applications/{manual["id"]}', headers=headers).json()['status'] == 'Interview'
    for model in (GmailConnection, GmailMessage, GmailReview, GmailSuppression):
        assert run(c, model.find_all().count()) == 0
    leftovers = run(c, ApplicationEvent.find_all().to_list())
    assert all(e.excerpt is None and e.message_id is None for e in leftovers)
    # No full body was ever stored, and the model never saw the user's name.
    assert all('Nowak' not in p for p in ai.prompts)
