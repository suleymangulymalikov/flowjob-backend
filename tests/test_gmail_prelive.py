"""Regression tests for the pre-live audit fixes (M1-M5, L1, L3, L4, L6, L7, L15). All mocked; no network."""
import asyncio
import base64
import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.applications.models import Application, ApplicationEvent
from app.config import settings
from app.gmail import reconcile, reviews as review_api, sync
from app.gmail.classify import employer_similar, title_grounded, employer_grounded
from app.gmail.connection import acquire_lock, disconnect, purge_if_disconnected
from app.gmail.google import GmailBadRequest, GoogleApi, IdTokenInvalid
from app.gmail.models import GmailConnection, GmailMessage, GmailReview, GmailSuppression
from app.gmail.preprocess import Minimized
from tests.gmail_fakes import BASE, connect, fake_google, signup  # noqa: F401
from tests.test_gmail_pipeline import (  # noqa: F401
    FakeAI, ai, apps, approve, email, events, item, manual_app, reviews, run, sync_now, world,
)
from tests.test_gmail_sync import Recorder, add_jobs, manual, recorder, the_conn  # noqa: F401


# --- M1: event-only late write after disconnect -----------------------------------------------------------------

def test_event_only_late_write_cannot_survive_disconnect(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    manual_id = client.post(f'{BASE}/applications', headers=headers,
                            json={'company': 'Acme', 'jobTitle': 'Analyst'}).json()['id']
    add_jobs(fake_google, 3, days_ago=0)
    recorder.seen.clear()

    async def disconnect_then_write(ctx, msg):
        if len(recorder.seen) == 1:
            await disconnect(await GmailConnection.get(conn.id))
            # The ONLY late write: activity with an excerpt on a manual application (no ledger/review/app ids).
            await ApplicationEvent(user_id=conn.user_id, application_id=manual_id, source='gmail',
                                   event_type='interview_invitation', occurred_at=datetime.now(UTC),
                                   connection_id=conn.id, message_id='deadbeef00000001', item_key='k',
                                   excerpt='We would like to invite you').insert()
    recorder.hook = disconnect_then_write
    summary = manual(client, conn.id)
    assert summary.stoppedReason == 'lock_lost'
    assert run(client, ApplicationEvent.find(ApplicationEvent.source == 'gmail').count()) == 0
    assert all(e.excerpt is None for e in run(client, ApplicationEvent.find_all().to_list()))


def test_cleanup_of_an_old_connection_never_touches_a_new_one(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    old = the_conn(client)
    assert client.delete(f'{BASE}/gmail', headers=headers).status_code == 204
    connect(client, headers)
    new = the_conn(client)
    run(client, GmailMessage(user_id=new.user_id, connection_id=new.id, message_id='m-new',
                             internal_at=datetime.now(UTC), state='processed').insert())
    # A stale worker of the old connection cleans up concurrently with the new connection's sync.
    assert run(client, purge_if_disconnected(old.user_id, old.id)) is True
    assert run(client, purge_if_disconnected(new.user_id, new.id)) is False
    assert run(client, GmailMessage.find(GmailMessage.connection_id == new.id).count()) == 1


# --- L3: approval racing disconnect -----------------------------------------------------------------------------

def test_approval_racing_disconnect_leaves_no_gmail_data(world, monkeypatch):
    w = world
    email(w, 'RACE', 'Hi, about the Junior Data Analyst role at Acme, are you interested in talking?',
          item(application_evidence='none', confidence='medium',
               supporting_quote='about the Junior Data Analyst role at Acme'))
    sync_now(w)
    [r] = reviews(w)
    original = reconcile.upsert_event

    async def disconnect_first(conn, *args, **kwargs):
        await disconnect(await GmailConnection.get(conn.id))  # the user disconnects in another tab mid-approval
        return await original(conn, *args, **kwargs)
    monkeypatch.setattr(reconcile, 'upsert_event', disconnect_first)
    res = approve(w, str(r.id))
    assert res.status_code == 409
    assert run(w['client'], ApplicationEvent.find(ApplicationEvent.source == 'gmail').count()) == 0
    for a in apps(w):
        assert a.gmail_message_id is None and a.gmail_connection_id is None and a.gmail_source_key is None
    for model in (GmailConnection, GmailReview, GmailMessage, GmailSuppression):
        assert run(w['client'], model.find_all().count()) == 0


# --- M2 + M4: linking needs independent evidence ----------------------------------------------------------------

def test_missing_employer_and_unnamed_company_is_never_linked_by_model_choice(world):
    w = world
    acme = manual_app(w, 'Acme', 'Data Analyst')
    manual_app(w, 'Beta', 'Data Analyst')
    email(w, 'NOEMP', 'Thanks, we received your documents for the Data Analyst position.',
          item(kind='existing_application_update', event_type='recruiter_follow_up', employer=None,
               job_title='Data Analyst', application_evidence='reference', match_key='A2',
               supporting_quote='we received your documents for the Data Analyst position'),
          sender='Recruiting <no-reply@ats.example>')
    sync_now(w)
    assert events(w) == []  # the same title at two companies is not enough, whatever the model picked
    [r] = reviews(w)
    assert r.kind == 'ambiguous_link' and acme in [str(c) for c in r.candidate_ids]


def test_missing_employer_but_company_named_in_email_links(world):
    w = world
    acme = manual_app(w, 'Acme', 'Data Analyst')
    manual_app(w, 'Beta', 'Data Analyst')
    email(w, 'NAMED', 'Acme Talent: thanks, we received your documents for the Data Analyst position.',
          item(kind='existing_application_update', event_type='recruiter_follow_up', employer=None,
               job_title='Data Analyst', application_evidence='reference', match_key='A1',
               supporting_quote='we received your documents for the Data Analyst position'))
    sync_now(w)
    assert [str(e.application_id) for e in events(w)] == [acme]


def test_model_choice_with_wrong_employer_goes_to_review(world):
    w = world
    manual_app(w, 'Acme', 'Data Analyst')
    beta = manual_app(w, 'Beta', 'Data Analyst')
    email(w, 'WRONG', 'Beta Recruiting: we would like to invite you to an interview for the Data Analyst role.',
          item(kind='existing_application_update', event_type='interview_invitation', employer='Beta',
               job_title='Data Analyst', application_evidence='active_process',
               supporting_quote='we would like to invite you to an interview', match_key='A2'))
    # Whatever key the model used, only the Beta application can be linked.
    sync_now(w)
    linked = {str(e.application_id) for e in events(w)}
    assert linked <= {beta}
    if not linked:
        assert reviews(w)[0].kind == 'ambiguous_link'


def test_competing_applications_at_one_employer_without_title_need_review(world):
    w = world
    manual_app(w, 'Acme', 'Data Analyst')
    manual_app(w, 'Acme', 'Backend Engineer')
    email(w, 'NOTITLE', 'Acme: please complete the online assessment by Friday.',
          item(kind='existing_application_update', event_type='assessment', job_title=None,
               application_evidence='active_process', supporting_quote='please complete the online assessment',
               match_key='A1'))
    sync_now(w)
    assert events(w) == [] and reviews(w)[0].kind == 'ambiguous_link'


def test_identical_reference_at_two_companies_links_only_the_right_employer(world):
    w = world
    email(w, 'R1', 'Thank you for applying to Acme for Junior Data Analyst (Ref R-100).',
          item(employer='Acme', job_title='Junior Data Analyst', references=['R-100'],
               supporting_quote='Thank you for applying to Acme for Junior Data Analyst'), minutes_ago=200)
    email(w, 'R2', 'Thank you for applying to Beta for Backend Intern (Ref R-100).',
          item(employer='Beta', job_title='Backend Intern', references=['R-100'],
               supporting_quote='Thank you for applying to Beta for Backend Intern'), minutes_ago=150)
    sync_now(w)
    by_company = {a.company: str(a.id) for a in apps(w)}
    assert set(by_company) == {'Acme', 'Beta'}
    email(w, 'R3', 'Beta: your application R-100 moves to the interview stage for Backend Intern.',
          item(kind='existing_application_update', event_type='interview_invitation', employer='Beta',
               job_title='Backend Intern', references=['R-100'], application_evidence='reference',
               supporting_quote='your application R-100 moves to the interview stage'), minutes_ago=30)
    sync_now(w)
    third = [e for e in events(w) if e.event_type == 'interview_invitation']
    assert [str(e.application_id) for e in third] == [by_company['Beta']]


def test_reference_without_employer_in_email_does_not_link_across_companies(world):
    w = world
    email(w, 'RA', 'Thank you for applying to Acme for Junior Data Analyst (Ref R-100).',
          item(employer='Acme', job_title='Junior Data Analyst', references=['R-100'],
               supporting_quote='Thank you for applying to Acme for Junior Data Analyst'), minutes_ago=200)
    email(w, 'RB', 'Thank you for applying to Beta for Backend Intern (Ref R-100).',
          item(employer='Beta', job_title='Backend Intern', references=['R-100'],
               supporting_quote='Thank you for applying to Beta for Backend Intern'), minutes_ago=150)
    sync_now(w)
    email(w, 'RC', 'Update on application R-100: next steps follow soon.',
          item(kind='existing_application_update', event_type='application_update', employer=None, job_title=None,
               references=['R-100'], application_evidence='reference',
               supporting_quote='Update on application R-100: next steps'), minutes_ago=30,
          sender='Recruiting <no-reply@ats.example>')
    sync_now(w)
    assert len(events(w)) == 2  # no third link: two companies share the reference
    assert reviews(w)[0].kind == 'ambiguous_link'


def test_l15_employer_comparison_is_whole_word():
    assert not employer_similar('ING', 'Booking.com')
    assert not employer_similar('Meta', 'Metaview')
    assert employer_similar('Acme', 'Acme Polska Sp. z o.o.')
    assert employer_similar('Allegro', 'Allegro.pl')
    assert employer_similar('Muster GmbH', 'Muster')


# --- M3: grounded automatic imports -----------------------------------------------------------------------------

def _reason(w):
    [r] = reviews(w)
    return r.proposal['reason']


def test_hallucinated_employer_is_not_imported(world):
    w = world
    email(w, 'HALL', 'Thank you for applying for Junior Data Analyst. We will review your application soon.',
          item(employer='Google', supporting_quote='Thank you for applying for Junior Data Analyst'))
    sync_now(w)
    assert apps(w) == [] and _reason(w) == 'ungrounded_employer'


def test_invented_title_is_not_imported(world):
    w = world
    email(w, 'TITLE', 'Thank you for applying to Acme. We will review your application soon.',
          item(job_title='Senior Machine Learning Engineer', supporting_quote='Thank you for applying to Acme'))
    sync_now(w)
    assert apps(w) == [] and _reason(w) == 'ungrounded_title'


def test_verified_reference_can_replace_title_wording(world):
    w = world
    email(w, 'REFT', 'Thank you for applying to Acme (requisition JR-5521). We will review it soon.',
          item(job_title='Data Analyst Intern', references=['JR-5521'], application_evidence='reference',
               event_type='application_update', supporting_quote='Thank you for applying to Acme (requisition'))
    sync_now(w)
    assert [a.job_title for a in apps(w)] == ['Data Analyst Intern']


def test_legitimate_contextual_polish_confirmation_with_sender_domain(world):
    w = world
    email(w, 'CTX', 'Dzień dobry, zapraszamy na rozmowę w sprawie stanowiska Młodszego Analityka Danych.',
          item(kind='submitted_application', event_type='interview_invitation', employer='Kowalski',
               job_title='Młodszy Analityk Danych', application_evidence='active_process',
               supporting_quote='zapraszamy na rozmowę w sprawie stanowiska'),
          subject='Rekrutacja CTX', sender='Zespół Rekrutacji <hr@kowalski.pl>')
    sync_now(w)
    assert [(a.company, a.job_title) for a in apps(w)] == [('Kowalski', 'Młodszy Analityk Danych')]


def test_misleading_advertisement_labelled_as_application_goes_to_review(world):
    w = world
    email(w, 'ADV', 'Acme is hiring! Apply now for Junior Data Analyst and start in January.',
          item(application_evidence='confirmation', supporting_quote='Apply now for Junior Data Analyst'))
    sync_now(w)
    assert apps(w) == [] and _reason(w) == 'possible_advertisement'


def test_short_generic_quote_is_not_enough(world):
    w = world
    email(w, 'SHORT', 'Thanks! Junior Data Analyst at Acme.',
          item(supporting_quote='Thanks! Junior'))
    sync_now(w)
    assert apps(w) == [] and _reason(w) == 'uncertain'


def test_grounding_helpers():
    mini = Minimized(text='Dziękujemy za aplikację na stanowisko Specjalisty ds. Analiz.', subject='Rekrutacja',
                     sender_name='HR', sender_domain='careers.kowalski-group.pl')
    assert employer_grounded('Kowalski Group S.A.', mini)
    assert not employer_grounded('Nowak Holding', mini)
    assert title_grounded('Specjalista ds. Analiz', mini)
    assert not title_grounded('Data Scientist', mini)


# --- M5: shutdown cancels running syncs -------------------------------------------------------------------------

def test_shutdown_cancels_running_sync_and_releases_the_lock(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    add_jobs(fake_google, 2, days_ago=0)
    started = asyncio.Event()

    async def block(ctx, msg):
        started.set()
        await asyncio.sleep(3600)
    recorder.hook = block

    async def scenario():
        owner = 'bg-owner'
        assert await acquire_lock(conn.id, owner)
        task = asyncio.create_task(sync.run_locked(conn.id, owner, trigger='manual', retry_failed=False))
        sync._tasks.add(task)
        task.add_done_callback(sync._tasks.discard)
        await asyncio.wait_for(started.wait(), 5)
        await sync.shutdown()
        doc = await GmailConnection.get_pymongo_collection().find_one({'_id': conn.id})
        return task.done(), doc['lock_owner'], await GmailMessage.find(GmailMessage.state == 'processing').count()
    done, lock_owner, processing = run(client, scenario())
    assert done and lock_owner is None  # no ghost lock after a reload
    assert processing == 1  # the interrupted message resumes on the next run
    recorder.hook = None
    manual(client, conn.id)
    assert run(client, GmailMessage.find(GmailMessage.state == 'processing').count()) == 0


# --- L4: rejected page token ------------------------------------------------------------------------------------

def test_rejected_page_token_restarts_the_window_once(client, fake_google, recorder, monkeypatch):
    monkeypatch.setattr(settings, 'gmail_max_list_pages_per_run', 1)
    fake_google.page_size = 10
    add_jobs(fake_google, 25)
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    assert conn.page_token == '1'
    original = fake_google.list_messages
    rejected = []

    async def reject_saved_token(token, query, page_token):
        if page_token == '1' and not rejected:  # the token saved by the previous run is no longer accepted
            rejected.append(page_token)
            raise GmailBadRequest()
        return await original(token, query, page_token)
    fake_google.list_messages = reject_saved_token
    monkeypatch.setattr(settings, 'gmail_max_list_pages_per_run', 10)
    manual(client, conn.id)
    conn = the_conn(client)
    assert rejected == ['1']
    assert conn.attention is None and conn.cursor_at is not None  # restarted from page 0, finished the window
    assert len(set(recorder.seen)) == 25 and len(recorder.seen) == 25  # every message once, none duplicated


def test_rejected_first_page_stops_without_looping(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)

    async def always_bad(token, query, page_token):
        raise GmailBadRequest()
    fake_google.list_messages = always_bad
    summary = manual(client, conn.id)
    assert summary.stoppedReason == 'gmail_bad_request'
    assert the_conn(client).attention == 'gmail_bad_request'


# --- L6: invalid key configuration ------------------------------------------------------------------------------

def test_invalid_token_key_is_reported_cleanly(client, fake_google, monkeypatch):
    headers = signup(client)
    monkeypatch.setattr(settings, 'gmail_token_key', 'not-a-valid-key')
    status = client.get(f'{BASE}/gmail', headers=headers).json()
    assert status['available'] is False
    r = client.post(f'{BASE}/gmail/oauth/start', headers=headers, json={'mode': 'connect'})
    assert r.status_code == 503 and 'misconfigured' in r.json()['detail']
    assert 'not-a-valid-key' not in r.text


def test_undecryptable_credential_needs_operator_attention(client, fake_google, recorder, monkeypatch):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    monkeypatch.setattr(settings, 'gmail_token_key', base64.b64encode(os.urandom(32)).decode())  # key rotated away
    summary = manual(client, conn.id)
    assert summary.stoppedReason == 'config_error'
    status = client.get(f'{BASE}/gmail', headers=headers).json()['sync']
    assert status['needsAttention'] == 'config_error' and 'misconfigured' in status['error']['message']


# --- L7: clock skew ---------------------------------------------------------------------------------------------

class _Jwks:
    def __init__(self, public):
        self.public = public

    def get_signing_key_from_jwt(self, token):
        return type('K', (), {'key': self.public})()


@pytest.mark.parametrize('iat_offset,exp_offset,ok', [
    (30, 300, True),     # issued 30 s "in the future" (slow server clock): accepted
    (0, -30, True),      # expired 30 s ago: within the 60 s tolerance
    (0, -120, False),    # expired 2 minutes ago: rejected
    (180, 600, False),   # issued 3 minutes in the future: rejected
])
def test_id_token_clock_skew_tolerance(fake_google, iat_offset, exp_offset, ok):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    claims = {'iss': 'https://accounts.google.com', 'aud': settings.google_oauth_client_id, 'sub': '1',
              'email': 'a@gmail.example', 'nonce': 'n', 'iat': now + timedelta(seconds=iat_offset),
              'exp': now + timedelta(seconds=exp_offset)}
    api = GoogleApi()
    api._jwks = _Jwks(key.public_key())
    token = jwt.encode(claims, key, algorithm='RS256')
    if ok:
        assert api._verify_id_token_sync(token, 'n').sub == '1'
    else:
        with pytest.raises(IdTokenInvalid):
            api._verify_id_token_sync(token, 'n')


# --- L1: interrupted status approval ----------------------------------------------------------------------------

def _pending_status(w):
    manual_app(w, 'Acme', 'Junior Data Analyst')
    email(w, 'L1', 'We would like to invite you to an interview for Junior Data Analyst at Acme.',
          item(kind='existing_application_update', event_type='interview_invitation', match_key='A1',
               application_evidence='active_process', proposed_status='Interview',
               supporting_quote='invite you to an interview for Junior Data Analyst'))
    sync_now(w)
    [r] = reviews(w)
    return r


def _interrupt_after_status_update(w, monkeypatch, r):
    async def crash(*args, **kwargs):
        raise RuntimeError('process died')
    monkeypatch.setattr(review_api, '_ensure_status_event', crash)
    with pytest.raises(RuntimeError):
        run(w['client'], review_api.approve(str(r.id), w['conn'].user_id, {}))
    monkeypatch.undo()


def test_interrupted_approval_retry_repairs_history(world, monkeypatch):
    w = world
    r = _pending_status(w)
    _interrupt_after_status_update(w, monkeypatch, r)
    assert [a.status.value for a in apps(w)] == ['Interview']
    assert [x.source for x in events(w) if x.event_type == 'status_change'] == []
    assert approve(w, str(r.id)).status_code == 200
    assert [x.status_to for x in events(w) if x.source == 'gmail_approved'] == ['Interview']
    assert reviews(w) == [] and len(reviews(w, 'approved')) == 1


def test_dismiss_after_interrupted_approval_records_the_truth(world, monkeypatch):
    w = world
    r = _pending_status(w)
    _interrupt_after_status_update(w, monkeypatch, r)
    res = w['client'].post(f'{BASE}/gmail/reviews/{r.id}/dismiss', headers=w['headers'])
    assert res.status_code == 409 and 'already approved' in res.json()['detail']
    assert len(reviews(w, 'approved')) == 1 and reviews(w, 'dismissed') == []
    assert [x.status_to for x in events(w) if x.source == 'gmail_approved'] == ['Interview']


def test_deletion_suppression_racing_disconnect_is_erased(world, monkeypatch):
    w = world
    email(w, 'SUPR', 'Thank you for applying for Junior Data Analyst at Acme.',
          item(supporting_quote='Thank you for applying for Junior Data Analyst'))
    sync_now(w)
    [a] = apps(w)
    original = GmailSuppression.insert

    async def disconnect_during_insert(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        # The disconnect's purge already ran just before this write landed.
        await GmailConnection.get_pymongo_collection().update_one({}, {'$set': {'status': 'disconnecting'}})
        return result
    monkeypatch.setattr(GmailSuppression, 'insert', disconnect_during_insert)
    assert w['client'].delete(f'{BASE}/applications/{a.id}', headers=w['headers']).status_code == 204
    assert run(w['client'], GmailSuppression.find_all().count()) == 0
