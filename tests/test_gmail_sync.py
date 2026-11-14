"""Checkpoint 2: enumeration, batching, persistence, locks, fencing, failures and the scheduler (all mocked)."""
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.gmail import sync
from app.gmail.connection import acquire_lock, disconnect
from app.gmail.google import GoogleUnavailable, InvalidGrant, MessageGone
from app.gmail.models import GmailConnection, GmailMessage, GmailReview, SchedulerLease
from app.gmail.preprocess import is_candidate, minimize
from app.gmail.scheduler import GmailScheduler
from tests.gmail_fakes import BASE, connect, fake_google, make_message, signup  # noqa: F401


def run(client, coro):
    async def wrapper():
        return await coro
    return client.portal.call(wrapper)


class Recorder:
    def __init__(self):
        self.seen: list[str] = []
        self.fail: set[str] = set()
        self.gone: set[str] = set()
        self.hook = None

    async def __call__(self, ctx, msg):
        self.seen.append(msg.message_id)
        if self.hook:
            await self.hook(ctx, msg)
        if msg.message_id in self.fail:
            raise sync.MessageFailed('ai_error')
        if msg.message_id in self.gone:
            return 'gone'
        return 'processed'


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(sync, 'processor', rec)
    return rec


def add_jobs(fake, n, *, start=0, days_ago=1, prefix='m'):
    base = datetime.now(UTC) - timedelta(days=days_ago)
    for i in range(start, start + n):
        mid = f'{prefix}{i:04d}'
        fake.messages[mid] = make_message(mid, subject=f'Your application #{i}', body='Thank you for applying',
                                          at=base - timedelta(minutes=i))


def ledger(client, **query):
    return run(client, GmailMessage.find(query).to_list())


def the_conn(client):
    return run(client, GmailConnection.find_one())


def scheduled(client, conn_id):
    return run(client, sync.run_now(conn_id, trigger='scheduled'))


def manual(client, conn_id, retry=False):
    return run(client, sync.run_now(conn_id, trigger='manual', retry_failed=retry))


# --- discovery -----------------------------------------------------------------------------------------------

def test_first_connection_backfills_30_days_and_filters_candidates(client, fake_google, recorder):
    now = datetime.now(UTC)
    fake_google.messages.update({
        'a1': make_message('a1', subject='Application received – Data Analyst', body='Thanks', at=now - timedelta(days=2)),
        'a2': make_message('a2', subject='Twoja aplikacja na stanowisko Analityk', body='Dziękujemy', at=now - timedelta(days=5)),
        'a3': make_message('a3', subject='Ihre Bewerbung bei Muster GmbH', body='Vielen Dank', at=now - timedelta(days=9)),
        'p1': make_message('p1', subject='Weekend sale', body='50% off shoes', sender='Shop <news@shop.example>',
                           at=now - timedelta(days=3), labels=['CATEGORY_PROMOTIONS']),
        'p2': make_message('p2', subject='New roles for you', body='Junior job openings near you',
                           sender='Jobs <alerts@board.example>', at=now - timedelta(days=3),
                           labels=['CATEGORY_PROMOTIONS']),
        'old': make_message('old', subject='Your application', body='x', at=now - timedelta(days=40)),
        'spam': make_message('spam', subject='Your application', body='x', at=now - timedelta(days=1), labels=['SPAM']),
        'ats': make_message('ats', subject='Update', body='Status changed', sender='no-reply@us.greenhouse-mail.io',
                            at=now - timedelta(days=4)),
    })
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    query = fake_google.queries[0]
    after = int(query.split('after:')[1].split()[0])
    assert abs(after - int((conn.connected_at.replace(tzinfo=UTC) - timedelta(days=30)).timestamp())) <= 2
    assert '-in:spam' in query and '-in:trash' in query and 'category' not in query  # Promotions included
    states = {m.message_id: m.state for m in ledger(client)}
    assert states == {'a1': 'processed', 'a2': 'processed', 'a3': 'processed', 'ats': 'processed',
                      'p2': 'processed', 'p1': 'skipped'}
    assert sorted(recorder.seen) == ['a1', 'a2', 'a3', 'ats', 'p2']
    assert recorder.seen[0] == 'a3'  # oldest first
    status = client.get(f'{BASE}/gmail', headers=headers).json()['sync']
    assert status['caughtUp'] is True and status['phase'] == 'incremental' and status['lastSuccessAt']
    assert status['lastRun']['processed'] == 5 and status['remaining'] == 0


def test_candidate_filter_unit():
    def meta(subject, sender='HR <hr@corp.example>', labels=None, snippet=''):
        return {'labelIds': labels or ['INBOX'], 'snippet': snippet,
                'payload': {'headers': [{'name': 'From', 'value': sender}, {'name': 'Subject', 'value': subject}]}}
    assert is_candidate(meta('Zaproszenie na rozmowę kwalifikacyjną'), 'me@x.example')
    assert is_candidate(meta('Einladung zum Vorstellungsgespräch'), 'me@x.example')
    assert is_candidate(meta('Hello', sender='Acme <notify@acme.myworkdayjobs.com>'), 'me@x.example')
    assert not is_candidate(meta('Your receipt from Coffee'), 'me@x.example')
    assert not is_candidate(meta('My application', sender='Me <me@x.example>'), 'me@x.example')
    assert not is_candidate(meta('Your application', labels=['TRASH']), 'me@x.example')


def test_more_than_50_candidates_use_multiple_batches(client, fake_google, recorder):
    add_jobs(fake_google, 60)
    headers = signup(client)
    connect(client, headers)
    assert len(recorder.seen) == 60 and len(set(recorder.seen)) == 60
    assert client.get(f'{BASE}/gmail', headers=headers).json()['sync']['caughtUp'] is True


def test_run_is_capped_at_200_and_the_rest_is_kept(client, fake_google, recorder):
    add_jobs(fake_google, 230)
    headers = signup(client)
    connect(client, headers)
    assert len(recorder.seen) == 200
    status = client.get(f'{BASE}/gmail', headers=headers).json()['sync']
    assert status['remaining'] == 30 and status['caughtUp'] is False and status['phase'] == 'backfill'
    scheduled(client, the_conn(client).id)
    assert len(recorder.seen) == 230 and len(set(recorder.seen)) == 230
    assert client.get(f'{BASE}/gmail', headers=headers).json()['sync']['caughtUp'] is True


def test_batch_size_and_count_are_bounded_by_configuration():
    from pydantic import ValidationError
    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(mongodb_uri='x', jwt_secret='y', gmail_batch_size=51)
    with pytest.raises(ValidationError):
        Settings(mongodb_uri='x', jwt_secret='y', gmail_max_batches_per_run=5)


def test_partial_pagination_persists_the_page_token(client, fake_google, recorder, monkeypatch):
    monkeypatch.setattr(settings, 'gmail_max_list_pages_per_run', 1)
    fake_google.page_size = 10
    add_jobs(fake_google, 25)
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    assert conn.page_token == '1' and conn.cursor_at is None and len(recorder.seen) == 10
    window = (conn.window_after, conn.window_before)
    manual(client, conn.id)
    conn = the_conn(client)
    assert (conn.window_after, conn.window_before) == window and conn.page_token == '2'
    manual(client, conn.id)
    conn = the_conn(client)
    assert conn.page_token is None and conn.cursor_at is not None and conn.window_after is None
    assert len(set(recorder.seen)) == 25


def test_failure_during_enumeration_resumes_from_saved_page(client, fake_google, recorder):
    fake_google.page_size = 10
    add_jobs(fake_google, 25)
    fake_google.fail_list_after_pages = 1
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    assert conn.attention == 'gmail_unavailable' and conn.page_token == '1' and conn.cursor_at is None
    assert len(ledger(client)) == 10  # the first page is durable
    fake_google.fail_list_after_pages = None
    manual(client, conn.id)
    conn = the_conn(client)
    assert conn.attention is None and conn.cursor_at is not None
    assert len(set(recorder.seen)) == 25


def test_restart_during_processing_resumes_claimed_work(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    run(client, GmailMessage(user_id=conn.user_id, connection_id=conn.id, message_id='crashed',
                             internal_at=datetime.now(UTC), state='processing', claim='dead-worker').insert())
    run(client, GmailMessage(user_id=conn.user_id, connection_id=conn.id, message_id='frozen',
                             internal_at=datetime.now(UTC), state='processing', claim='dead-worker',
                             analysis={'items': []}).insert())
    scheduled(client, conn.id)
    assert {'crashed', 'frozen'} <= set(recorder.seen)
    assert {m.state for m in ledger(client)} == {'processed'}


def test_overlap_and_duplicates_are_processed_once(client, fake_google, recorder):
    add_jobs(fake_google, 5)
    headers = signup(client)
    connect(client, headers)
    run(client, GmailConnection.get_pymongo_collection().update_many({}, {'$set': {'last_manual_sync_at': None}}))
    manual(client, the_conn(client).id)  # incremental window overlaps the backfill by two days
    assert len(fake_google.queries) == 2
    assert sorted(recorder.seen) == sorted(set(recorder.seen)) and len(recorder.seen) == 5


def test_deleted_messages_are_handled(client, fake_google, recorder):
    add_jobs(fake_google, 3)
    fake_google.get_errors['m0001'] = MessageGone()  # deleted between listing and metadata
    recorder.gone.add('m0002')  # deleted before the full fetch
    headers = signup(client)
    connect(client, headers)
    states = {m.message_id: m.state for m in ledger(client)}
    assert states == {'m0000': 'processed', 'm0002': 'gone'}


# --- locks and fencing --------------------------------------------------------------------------------------

def test_lock_contention_and_expired_lock(client, fake_google, recorder, monkeypatch):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    assert run(client, acquire_lock(conn.id, 'other-worker'))
    assert run(client, acquire_lock(conn.id, 'me')) is False
    assert manual(client, conn.id) is None
    run(client, GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn.id}, {'$set': {'last_manual_sync_at': None}}))
    r = client.post(f'{BASE}/gmail/sync', headers=headers, json={})
    assert r.status_code == 409 and 'already running' in r.json()['detail']
    assert client.get(f'{BASE}/gmail', headers=headers).json()['sync']['running'] is True
    run(client, GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn.id}, {'$set': {'lock_expires_at': datetime.now(UTC) - timedelta(seconds=1)}}))
    assert manual(client, conn.id) is not None  # an expired lock can be taken over


def test_stale_worker_stops_writing_after_losing_the_lock(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    add_jobs(fake_google, 5, days_ago=0)
    recorder.seen.clear()

    async def steal(ctx, msg):
        if len(recorder.seen) == 2:
            # Simulate this worker stalling past its TTL while another worker takes over.
            await GmailConnection.get_pymongo_collection().update_one(
                {'_id': conn.id}, {'$set': {'lock_owner': 'new-owner',
                                            'lock_expires_at': datetime.now(UTC) + timedelta(minutes=10)}})
    recorder.hook = steal
    summary = manual(client, conn.id)
    assert summary.stoppedReason == 'lock_lost'
    assert len(recorder.seen) == 2
    rows = {m.message_id: (m.state, m.claim) for m in ledger(client)}
    # The message being handled when the lock was lost was not settled by the stale worker.
    assert sum(1 for s, _ in rows.values() if s == 'processed') == 1
    assert the_conn(client).lock_owner == 'new-owner'  # the stale worker did not release someone else's lock


# --- failures ------------------------------------------------------------------------------------------------

def test_failed_messages_wait_for_manual_retry(client, fake_google, recorder):
    add_jobs(fake_google, 3)
    recorder.fail.add('m0001')
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    status = client.get(f'{BASE}/gmail', headers=headers).json()['sync']
    assert status['failed'] == 1 and status['caughtUp'] is True  # failed work is not "remaining"
    recorder.fail.clear()
    recorder.seen.clear()
    scheduled(client, conn.id)
    assert 'm0001' not in recorder.seen  # never replayed automatically
    run(client, GmailConnection.get_pymongo_collection().update_one({'_id': conn.id},
                                                                    {'$set': {'last_manual_sync_at': None}}))
    assert client.post(f'{BASE}/gmail/sync', headers=headers, json={'retryFailed': True}).status_code == 202
    assert recorder.seen == ['m0001']
    assert client.get(f'{BASE}/gmail', headers=headers).json()['sync']['failed'] == 0


def test_connection_wide_failure_needs_attention_and_scheduler_skips(client, fake_google, recorder):
    headers = signup(client)
    fake_google.list_error = GoogleUnavailable()
    connect(client, headers)
    conn = the_conn(client)
    status = client.get(f'{BASE}/gmail', headers=headers).json()['sync']
    assert status['needsAttention'] == 'gmail_unavailable' and status['error']['code'] == 'gmail_unavailable'
    run(client, GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn.id}, {'$set': {'last_sync_started_at': datetime.now(UTC) - timedelta(hours=4)}}))
    calls = fake_google.list_calls
    assert run(client, GmailScheduler().tick()) == 0 and fake_google.list_calls == calls
    fake_google.list_error = None
    run(client, GmailConnection.get_pymongo_collection().update_one({'_id': conn.id},
                                                                    {'$set': {'last_manual_sync_at': None}}))
    assert client.post(f'{BASE}/gmail/sync', headers=headers, json={}).status_code == 202
    assert client.get(f'{BASE}/gmail', headers=headers).json()['sync']['needsAttention'] is None


def test_authorization_expiry_requires_reconnect(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    fake_google.refresh_error = InvalidGrant()
    summary = manual(client, conn.id)
    assert summary.stoppedReason == 'reauth_required'
    status = client.get(f'{BASE}/gmail', headers=headers).json()
    assert status['connection']['state'] == 'reauth_required'
    run(client, GmailConnection.get_pymongo_collection().update_one({'_id': conn.id},
                                                                    {'$set': {'last_manual_sync_at': None}}))
    r = client.post(f'{BASE}/gmail/sync', headers=headers, json={})
    assert r.status_code == 409 and 'Reconnect' in r.json()['detail']
    assert run(client, GmailScheduler().due_connections()) == []


def test_manual_sync_is_rate_limited_and_owner_scoped(client, fake_google, recorder):
    headers = signup(client, 'a@example.com')
    connect(client, headers)
    first = client.post(f'{BASE}/gmail/sync', headers=headers, json={})
    assert first.status_code == 202 and first.json()['connection']['state'] == 'active'
    second = client.post(f'{BASE}/gmail/sync', headers=headers, json={})
    assert second.status_code == 429 and second.headers['retry-after']
    other = signup(client, 'b@example.com')
    assert client.post(f'{BASE}/gmail/sync', headers=other, json={}).status_code == 404


# --- scheduler -----------------------------------------------------------------------------------------------

def test_scheduler_single_ownership_and_due_selection(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    one, two = GmailScheduler(), GmailScheduler()
    assert run(client, one.hold_lease()) is True
    assert run(client, two.hold_lease()) is False
    assert run(client, two.tick()) == 0
    assert run(client, one.tick()) == 0  # just synced: not due yet
    run(client, GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn.id}, {'$set': {'last_sync_started_at': datetime.now(UTC) - timedelta(hours=3, minutes=1)}}))
    assert run(client, one.tick()) == 1
    # Lease expiry hands ownership over; the old owner cannot renew afterwards.
    run(client, SchedulerLease.get_pymongo_collection().update_one(
        {'_id': 'gmail-scheduler'}, {'$set': {'expires_at': datetime.now(UTC) - timedelta(seconds=1)}}))
    assert run(client, two.hold_lease()) is True
    assert run(client, one.hold_lease()) is False
    run(client, two.release())
    assert run(client, SchedulerLease.find_all().count()) == 0


def test_scheduler_is_disabled_by_default():
    assert settings.gmail_scheduler_enabled is False


def test_scheduler_start_and_stop_release_the_lease(client, fake_google, recorder, monkeypatch):
    monkeypatch.setattr(settings, 'gmail_scheduler_tick_seconds', 3600)
    s = GmailScheduler()

    async def lifecycle():
        import asyncio
        s.start()
        await asyncio.sleep(0.05)
        held = await SchedulerLease.find_all().count()
        await s.stop()
        return held, await SchedulerLease.find_all().count()
    assert run(client, lifecycle()) == (1, 0)


# --- disconnect during a run ---------------------------------------------------------------------------------

def test_disconnect_during_a_run_erases_late_writes(client, fake_google, recorder):
    headers = signup(client)
    connect(client, headers)
    conn = the_conn(client)
    add_jobs(fake_google, 3, days_ago=0)
    recorder.seen.clear()

    async def disconnect_midway(ctx, msg):
        if len(recorder.seen) == 1:
            await disconnect(await GmailConnection.get(conn.id))
            # A write the worker makes after the disconnect's purge:
            await GmailReview(user_id=conn.user_id, connection_id=conn.id, message_id=msg.message_id,
                              item_key='x', kind='new_application', internal_at=datetime.now(UTC)).insert()
    recorder.hook = disconnect_midway
    summary = manual(client, conn.id)
    assert summary.stoppedReason == 'lock_lost' and len(recorder.seen) == 1
    for model in (GmailConnection, GmailMessage, GmailReview):
        assert run(client, model.find_all().count()) == 0
    assert client.get(f'{BASE}/gmail', headers=headers).json()['connection']['state'] == 'not_connected'


# --- minimization --------------------------------------------------------------------------------------------

def test_minimize_strips_history_signature_contacts_and_attachments():
    body = (
        'Dear Ana Nowak,\n\nThank you for applying for Junior Data Analyst (Req ID R-20931) at Acme.\n'
        'Your interview is on 12 March 2026 at 10:00 CET.\nCall +48 601 234 567 or mail ana.nowak@uni.example.\n'
        'Details: https://acme.wd3.myworkdayjobs.com/en-US/careers/job/R-20931?token=abcdefghijklmnopqrstuvwxyz123456\n'
        '--\nJane Recruiter\nTalent Acquisition\n\nOn Mon, 3 Mar 2026, Ana wrote:\n> my previous email'
    )
    message = make_message('x', subject='Interview – Ana Nowak', body=body, attachment=True)
    out = minimize(message, user_name='Ana Nowak', user_email='ana.nowak@uni.example')
    assert 'R-20931' in out.text and '12 March 2026 at 10:00 CET' in out.text and 'Acme' in out.text
    import re
    assert not re.search(r'\bAna\b', out.text) and 'Nowak' not in out.text and '[you]' in out.text
    assert 'Junior Data Analyst' in out.text
    assert '601 234 567' not in out.text and 'ana.nowak@uni.example' not in out.text
    assert 'token=' not in out.text and 'abcdefghijklmnopqrstuvwxyz' not in out.text
    assert 'myworkdayjobs.com' in out.text and 'Jane Recruiter' not in out.text
    assert 'previous email' not in out.text and 'ATT-SECRET' not in out.text and 'offer.pdf' not in out.text
    assert out.subject == 'Interview – [you]' and out.sender_domain == 'acme.example'


def test_minimize_html_and_multilingual_quotes():
    html = ('<html><style>.x{}</style><body><p>Dziękujemy za aplikację na stanowisko <b>Młodszy Analityk</b>.</p>'
            '<div class="gmail_quote">W dniu 1 marca Ana napisała: stare</div>'
            '<p>Wypisz się z newslettera</p></body></html>')
    out = minimize(make_message('h', subject='Rekrutacja', body=html, html=True), user_name='Ana', user_email='a@x.example')
    assert 'Młodszy Analityk' in out.text and 'stare' not in out.text and 'Wypisz' not in out.text
    german = 'Vielen Dank für Ihre Bewerbung als Werkstudent.\n\nAm 2. März 2026 schrieb Ana <a@x.example>:\n> alt'
    out = minimize(make_message('g', subject='Bewerbung', body=german), user_name='Ana', user_email='a@x.example')
    assert 'Werkstudent' in out.text and 'alt' not in out.text
