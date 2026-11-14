"""Pre-merge hardening: interrupted disconnects (M1), temporary Gmail failures (M2), reconnect behaviour (M3), time
zones (M4) and small release items (revocation logging, access-log redaction, email_verified). All mocked."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.applications.models import Application, ApplicationEvent
from app.gmail import connection as connection_module
from app.gmail import google as google_module
from app.gmail import sync
from app.gmail.classify import parse_when
from app.gmail.connection import acquire_lock, disconnect, finish_abandoned_disconnects, finish_disconnect
from app.gmail.google import GoogleApi, GoogleRateLimited, GoogleRejected, GoogleUnavailable, temporary_error
from app.gmail.models import GmailConnection, GmailMessage, GmailOAuthState, GmailReview, GmailSuppression
from app.gmail.oauth import RedactOAuthCallback
from app.gmail.scheduler import GmailScheduler
from tests.gmail_fakes import BASE, connect, fake_google, signup, start_flow  # noqa: F401
from tests.test_gmail_pipeline import (  # noqa: F401
    FakeAI, ai, api_apps, apps, approve, email, events, item, reviews, run, sync_now, world,
)

GMAIL_MODELS = (GmailConnection, GmailMessage, GmailReview, GmailSuppression, GmailOAuthState)


def conn_doc(w):
    return run(w['client'], GmailConnection.find_one())


def status(w):
    return w['client'].get(f'{BASE}/gmail', headers=w['headers']).json()


def gmail_counts(w):
    counts = {m.__name__: run(w['client'], m.find_all().count()) for m in GMAIL_MODELS}
    counts['gmail_events'] = run(w['client'], ApplicationEvent.find(ApplicationEvent.source == 'gmail').count())
    return counts


def assert_gmail_erased(w):
    assert set(gmail_counts(w).values()) == {0}, gmail_counts(w)


def imported_app(w, marker='IMP'):
    email(w, marker, f'Thank you for applying for Junior Data Analyst at Acme. {marker}',
          item(supporting_quote='Thank you for applying for Junior Data Analyst'))
    sync_now(w)
    [a] = apps(w)
    return a


# =================================================================================================================
# M1: interrupted disconnects
# =================================================================================================================

def interrupt_purge(monkeypatch, times=1):
    """Simulate a process/database interruption in the middle of erasure: some rows go, then it fails."""
    real = connection_module.purge
    calls = {'n': 0}

    async def flaky(user_id, connection_id):
        calls['n'] += 1
        if calls['n'] <= times:
            await GmailMessage.get_pymongo_collection().delete_many({'connection_id': connection_id})
            raise RuntimeError('interrupted')
        await real(user_id, connection_id)
    monkeypatch.setattr(connection_module, 'purge', flaky)
    return calls


def test_interrupted_disconnect_is_visible_and_safely_retried(world, monkeypatch):
    w = world
    imported_app(w)
    interrupt_purge(monkeypatch)
    r = w['client'].delete(f'{BASE}/gmail', headers=w['headers'])
    assert r.status_code == 503 and 'Try again' in r.json()['detail']
    # The connection (with its encrypted credential) is kept for the retry, and clearly reported.
    s = status(w)
    assert s['connection'] == {'state': 'disconnecting'} and s['sync'] is None and s['pendingReviewCount'] == 0
    # While disconnecting, nothing can sync or reconnect.
    assert run(w['client'], acquire_lock(conn_doc(w).id, 'someone')) is False
    assert w['client'].post(f'{BASE}/gmail/sync', headers=w['headers'], json={}).status_code == 404
    r = w['client'].post(f'{BASE}/gmail/oauth/start', headers=w['headers'], json={'mode': 'connect'})
    assert r.status_code == 409 and 'being disconnected' in r.json()['detail']
    # User retry finishes it.
    assert w['client'].delete(f'{BASE}/gmail', headers=w['headers']).status_code == 204
    assert_gmail_erased(w)
    assert status(w)['connection']['state'] == 'not_connected'
    assert len(apps(w)) == 1  # applications stay
    assert len(w['google'].revoked) == 2  # revocation was retried too (best effort, idempotent at Google)


def test_scheduler_finishes_abandoned_disconnects_only_after_a_grace_period(world, monkeypatch):
    w = world
    imported_app(w)
    interrupt_purge(monkeypatch)
    assert w['client'].delete(f'{BASE}/gmail', headers=w['headers']).status_code == 503
    scheduler = GmailScheduler()
    # A disconnect that may still be running in another process is left alone.
    run(w['client'], scheduler.tick())
    assert conn_doc(w).status == 'disconnecting'
    # Once abandoned (process died), the scheduler completes it without the user.
    run(w['client'], GmailConnection.get_pymongo_collection().update_many(
        {}, {'$set': {'updated_at': datetime.now(UTC) - timedelta(minutes=6)}}))
    run(w['client'], scheduler.tick())
    assert_gmail_erased(w)
    assert len(apps(w)) == 1


def test_repeated_cleanup_is_idempotent(world):
    w = world
    imported_app(w)
    conn = conn_doc(w)
    run(w['client'], GmailConnection.get_pymongo_collection().update_one(
        {'_id': conn.id}, {'$set': {'status': 'disconnecting', 'lock_owner': 'disconnect',
                                    'updated_at': datetime.now(UTC) - timedelta(hours=1)}}))
    run(w['client'], finish_disconnect(conn))
    run(w['client'], finish_disconnect(conn))  # again: nothing left, no error
    assert run(w['client'], finish_abandoned_disconnects()) == 0
    assert_gmail_erased(w)
    assert len(apps(w)) == 1


def test_a_failing_abandoned_cleanup_is_retried_next_tick(world, monkeypatch):
    w = world
    imported_app(w)
    interrupt_purge(monkeypatch, times=2)
    assert w['client'].delete(f'{BASE}/gmail', headers=w['headers']).status_code == 503
    run(w['client'], GmailConnection.get_pymongo_collection().update_many(
        {}, {'$set': {'updated_at': datetime.now(UTC) - timedelta(minutes=10)}}))
    assert run(w['client'], finish_abandoned_disconnects()) == 0  # fails again: logged, kept
    assert conn_doc(w).status == 'disconnecting'
    assert run(w['client'], finish_abandoned_disconnects()) == 1
    assert_gmail_erased(w)


def test_disconnect_does_not_delete_a_reconnected_active_connection(world):
    """The final delete only removes a connection still marked disconnecting (no cross-flow damage)."""
    w = world
    conn = conn_doc(w)
    run(w['client'], disconnect(conn))
    assert conn_doc(w) is None
    connect(w['client'], w['headers'])
    fresh = conn_doc(w)
    revoked = len(w['google'].revoked)
    run(w['client'], finish_disconnect(conn))  # a stale retry of the old flow
    assert conn_doc(w) is not None and conn_doc(w).id == fresh.id and conn_doc(w).status == 'active'
    assert len(w['google'].revoked) == revoked  # never revokes again for a finished disconnect


@pytest.mark.parametrize('result,raised,expected', [
    (False, None, 'did not confirm revocation'),
    (True, GoogleUnavailable(), 'gmail_unavailable'),
])
def test_failed_revocation_is_logged_without_sensitive_data(world, caplog, result, raised, expected):
    w = world
    w['google'].revoke_result, w['google'].revoke_error = result, raised
    with caplog.at_level(logging.WARNING, logger='app.gmail.connection'):
        assert w['client'].delete(f'{BASE}/gmail', headers=w['headers']).status_code == 204
    text = caplog.text
    assert expected in text
    assert 'refresh-token-1' not in text and 'student@gmail.example' not in text
    assert_gmail_erased(w)  # erasure never depends on Google confirming


def test_revoke_maps_google_responses(monkeypatch):
    def handler_for(status_code, body):
        return lambda request: httpx.Response(status_code, json=body)
    for code, body, ok in ((200, {}, True), (400, {'error': 'invalid_token'}, True),
                           (400, {'error': 'unsupported_token_type'}, False), (503, {}, False)):
        mock_http(monkeypatch, handler_for(code, body))
        assert asyncio.run(GoogleApi().revoke('t')) is ok


# =================================================================================================================
# M2: temporary Gmail failures
# =================================================================================================================

REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_http(monkeypatch, handler):
    monkeypatch.setattr(google_module.httpx, 'AsyncClient',
                        lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw))


def _resp(code, body=None, headers=None):
    return httpx.Response(code, json=body if body is not None else {}, headers=headers or {})


def _gmail_error(reason, code=403):
    return {'error': {'code': code, 'status': 'PERMISSION_DENIED', 'errors': [{'reason': reason}]}}


def test_temporary_error_classification():
    limited = temporary_error(_resp(429, headers={'Retry-After': '120'}))
    assert isinstance(limited, GoogleRateLimited) and limited.retry_after == 120
    assert isinstance(temporary_error(_resp(403, _gmail_error('rateLimitExceeded'))), GoogleRateLimited)
    assert isinstance(temporary_error(_resp(403, _gmail_error('userRateLimitExceeded'))), GoogleRateLimited)
    assert isinstance(temporary_error(_resp(503)), GoogleUnavailable)
    assert not isinstance(temporary_error(_resp(503)), GoogleRateLimited)
    # Permanent authorization problems are not temporary.
    assert temporary_error(_resp(403, _gmail_error('insufficientPermissions'))) is None
    assert temporary_error(_resp(403, {'error': 'not json shaped'})) is None
    assert temporary_error(_resp(401)) is None


def test_retry_after_http_date_and_caps():
    future = (datetime.now(UTC) + timedelta(minutes=10)).strftime('%a, %d %b %Y %H:%M:%S GMT')
    assert 590 <= temporary_error(_resp(429, headers={'Retry-After': future})).retry_after <= 600
    assert temporary_error(_resp(429, headers={'Retry-After': '9999999'})).retry_after == 24 * 3600
    assert temporary_error(_resp(429, headers={'Retry-After': 'soon'})).retry_after is None


@pytest.mark.parametrize('status_code,body,expected', [
    (429, {}, GoogleRateLimited),
    (403, _gmail_error('rateLimitExceeded'), GoogleRateLimited),
    (500, {}, GoogleUnavailable),
    (403, _gmail_error('insufficientPermissions'), GoogleRejected),
    (401, {}, GoogleRejected),
])
def test_gmail_api_maps_http_failures(monkeypatch, status_code, body, expected):
    mock_http(monkeypatch, lambda request: _resp(status_code, body))
    with pytest.raises(expected) as raised:
        asyncio.run(GoogleApi().list_messages('token', 'q', None))
    assert type(raised.value) is expected


def test_network_failure_is_temporary(monkeypatch):
    def boom(request):
        raise httpx.ConnectError('down')
    mock_http(monkeypatch, boom)
    with pytest.raises(GoogleUnavailable):
        asyncio.run(GoogleApi().get_full('token', 'abc'))


def test_token_endpoint_rate_limit_is_temporary_not_reauth(monkeypatch):
    mock_http(monkeypatch, lambda request: _resp(429, headers={'Retry-After': '30'}))
    with pytest.raises(GoogleRateLimited):
        asyncio.run(GoogleApi().refresh('r'))


def test_rate_limit_backs_off_respects_retry_after_and_keeps_the_connection(world, monkeypatch):
    w = world
    monkeypatch.setattr(sync.settings, 'gmail_scheduler_enabled', True)
    w['google'].list_error = GoogleRateLimited(retry_after=3600)
    sync_now(w)
    conn = conn_doc(w)
    assert conn.status == 'active' and conn.attention is None and conn.transient_failures == 1
    wait = conn.retry_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(minutes=59) < wait <= timedelta(hours=1)  # Retry-After beats the 15-minute backoff
    s = status(w)
    assert s['connection']['state'] == 'active'  # never "reconnect" for a temporary failure
    assert s['sync']['error']['code'] == 'gmail_rate_limited' and 'retries automatically' in s['sync']['error']['message']
    assert s['sync']['retryAt'] is not None
    # Manual syncs respect Google's wait too.
    run(w['client'], GmailConnection.get_pymongo_collection().update_one({}, {'$set': {'last_manual_sync_at': None}}))
    r = w['client'].post(f'{BASE}/gmail/sync', headers=w['headers'], json={})
    assert r.status_code == 429 and 3500 <= int(r.headers['retry-after']) <= 3600
    # The scheduler waits for the backoff, then retries; success clears the incident.
    scheduler = GmailScheduler()
    assert run(w['client'], scheduler.due_connections()) == []
    w['google'].list_error = None
    run(w['client'], GmailConnection.get_pymongo_collection().update_one(
        {}, {'$set': {'retry_at': datetime.now(UTC) - timedelta(seconds=1)}}))
    assert run(w['client'], scheduler.tick()) == 1
    conn = conn_doc(w)
    assert conn.retry_at is None and conn.transient_failures == 0 and conn.attention is None


def test_transient_retries_are_bounded_with_exponential_backoff(world):
    w = world
    w['google'].list_error = GoogleUnavailable()
    scheduler = GmailScheduler()
    sync_now(w)  # failure 1 (user-started)
    waits = []
    for expected in range(1, 6):
        conn = conn_doc(w)
        assert conn.transient_failures == expected and conn.attention is None
        waits.append(round((conn.retry_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds() / 60))
        run(w['client'], GmailConnection.get_pymongo_collection().update_one(
            {}, {'$set': {'retry_at': datetime.now(UTC) - timedelta(seconds=1)}}))
        assert run(w['client'], scheduler.tick()) == 1  # an automatic retry
    assert waits == [15, 30, 60, 120, 240]
    conn = conn_doc(w)
    # Sixth consecutive failure: stop retrying automatically; the user decides when to try again.
    assert conn.attention == 'gmail_unavailable' and conn.retry_at is None
    calls = w['google'].list_calls
    assert run(w['client'], scheduler.tick()) == 0 and w['google'].list_calls == calls
    assert status(w)['connection']['state'] == 'active'


def test_temporary_token_refresh_failure_does_not_require_reconnect(world):
    w = world
    w['google'].refresh_error = GoogleUnavailable()
    summary = sync_now(w)
    assert summary.stoppedReason == 'gmail_unavailable'
    conn = conn_doc(w)
    assert conn.status == 'active' and conn.attention is None and conn.retry_at is not None


def test_message_fetch_rate_limit_keeps_the_message_queued(world):
    w = world
    mid = email(w, 'RL', 'Thank you for applying for Junior Data Analyst at Acme.',
                item(supporting_quote='Thank you for applying for Junior Data Analyst'))
    w['google'].get_errors[mid] = GoogleRateLimited()
    sync_now(w)  # metadata fetch is rate limited: nothing recorded, retried later
    assert run(w['client'], GmailMessage.find_all().count()) == 0 and conn_doc(w).retry_at is not None
    del w['google'].get_errors[mid]
    run(w['client'], GmailConnection.get_pymongo_collection().update_one(
        {}, {'$set': {'retry_at': datetime.now(UTC) - timedelta(seconds=1)}}))
    assert run(w['client'], GmailScheduler().tick()) == 1
    assert len(apps(w)) == 1


# =================================================================================================================
# M3: reconnect after disconnect
# =================================================================================================================

GBE = 'Graduate Backend Engineer'
REF = 'WT-GBE-77'


def gbe_item(**kw):
    base = dict(employer='WhyTech', job_title=GBE, references=[REF])
    base.update(kw)
    return item(**base)


def build_closed_history(w):
    """Confirmation → interview → offer → rejection, each status approved by the user; plus one dismissed review."""
    email(w, 'GBE-CONF', f'Thank you for applying for {GBE} at WhyTech. Reference {REF}.',
          gbe_item(supporting_quote=f'Thank you for applying for {GBE} at WhyTech'), minutes_ago=240)
    steps = [('GBE-INT', 'interview_invitation', 'Interview', 'We would like to invite you to an interview', 200),
             ('GBE-OFF', 'offer', 'Offer', 'We are happy to make you an offer', 160),
             ('GBE-REJ', 'rejection', 'Rejected', 'Unfortunately we have decided not to proceed', 120)]
    for marker, event, status_to, quote, ago in steps:
        email(w, marker, f'{quote} for the {GBE} role (reference {REF}) at WhyTech.',
              gbe_item(kind='existing_application_update', event_type=event, application_evidence='active_process',
                       proposed_status=status_to, supporting_quote=quote), minutes_ago=ago)
    email(w, 'AD', 'Globex is hiring a Platform Engineer. Thank you for applying to our talent pool.',
          item(employer='Globex', job_title='Platform Engineer', confidence='low',
               supporting_quote='Thank you for applying to our talent pool'), minutes_ago=100)
    sync_now(w)
    for _ in range(3):  # approve the pending status suggestion each time (only the newest stays pending)
        sync_now(w)
        pending = [r for r in reviews(w) if r.kind == 'status_change']
        if not pending:
            break
        for r in pending:
            approve(w, str(r.id))
    [ad] = [r for r in reviews(w) if r.kind == 'new_application']
    assert w['client'].post(f'{BASE}/gmail/reviews/{ad.id}/dismiss', headers=w['headers']).status_code == 200


def test_reconnect_does_not_raise_false_reapplication_or_status_suggestions(world):
    w = world
    build_closed_history(w)
    [gbe] = [a for a in api_apps(w) if a['jobTitle'] == GBE]
    if gbe['status'] != 'Rejected':  # older suggestions are superseded; approve the newest directly
        pytest.fail(f'setup did not reach Rejected: {gbe["status"]}')
    history = [h['status'] for h in gbe['history']]
    # Disconnect: every Gmail-derived record goes, no fingerprints are kept.
    assert w['client'].delete(f'{BASE}/gmail', headers=w['headers']).status_code == 204
    assert_gmail_erased(w)
    # Reconnect the same mailbox: the last 30 days are read again.
    connect(w['client'], w['headers'])
    w['conn'] = conn_doc(w)
    sync_now(w)
    pending = reviews(w)
    reasons = [r.proposal.get('reason') for r in pending]
    assert 'possible_reapplication' not in reasons  # historical confirmation predates the approved decisions
    assert not [r for r in pending if r.kind == 'status_change']  # nothing older than the decisions is proposed
    after = [a for a in api_apps(w) if a['jobTitle'] == GBE]
    assert len(after) == 1 and after[0]['status'] == 'Rejected'
    assert [h['status'] for h in after[0]['history']] == history  # approved history preserved
    assert len(apps(w)) == 1
    # Disclosed behaviour: the dismissed suggestion may reappear, because its dismissal was erased with Gmail data.
    assert reasons.count('low_confidence') == 1


def test_genuine_reapplication_after_rejection_is_still_reviewed(world):
    w = world
    build_closed_history(w)
    email(w, 'GBE-NEW', f'Thank you for applying for {GBE} at WhyTech. We received your new application.',
          item(employer='WhyTech', job_title=GBE, supporting_quote=f'Thank you for applying for {GBE} at WhyTech'),
          minutes_ago=5)
    sync_now(w)
    assert [r.proposal['reason'] for r in reviews(w) if r.kind == 'new_application'
            and r.proposal.get('reason') == 'possible_reapplication']


def test_deleted_application_stays_suppressed_while_connected(world):
    w = world
    a = imported_app(w, 'SUP1')
    assert w['client'].delete(f'{BASE}/applications/{a.id}', headers=w['headers']).status_code == 204
    run(w['client'], GmailMessage.get_pymongo_collection().update_many({}, {'$set': {'state': 'failed',
                                                                                     'analysis': None}}))
    sync_now(w, retry=True)
    assert apps(w) == [] and reviews(w) == []


# =================================================================================================================
# M4: time zones
# =================================================================================================================

WARSAW = 'Europe/Warsaw'


@pytest.mark.parametrize('value,stated,utc,label', [
    ('2026-11-19T11:00', 'UTC+2', '2026-11-19T09:00:00+00:00', 'UTC+02:00'),
    ('2026-11-19T11:00', 'GMT+1', '2026-11-19T10:00:00+00:00', 'UTC+01:00'),
    ('2026-11-19T11:00', 'UTC−05:00', '2026-11-19T16:00:00+00:00', 'UTC-05:00'),
    ('2026-11-19T11:00', 'utc -5', '2026-11-19T16:00:00+00:00', 'UTC-05:00'),
    ('2026-11-19T11:00', '+05:30', '2026-11-19T05:30:00+00:00', 'UTC+05:30'),
    ('2026-11-19T11:00', 'UTC', '2026-11-19T11:00:00+00:00', 'UTC+00:00'),
    ('2026-11-19T11:00', 'CET', '2026-11-19T10:00:00+00:00', 'CET'),
    ('2026-11-19T11:00', 'America/New_York', '2026-11-19T16:00:00+00:00', 'America/New_York'),
    # No stated zone: the user's zone, labelled as such (winter and summer time).
    ('2026-11-19T11:00', None, '2026-11-19T10:00:00+00:00', WARSAW),
    ('2026-07-01T11:00', None, '2026-07-01T09:00:00+00:00', WARSAW),
    # An offset in the value is authoritative and labels itself (not the user's zone).
    ('2026-11-19T11:00-05:00', None, '2026-11-19T16:00:00+00:00', 'UTC-05:00'),
    ('2026-11-19T11:00+01:00', WARSAW, '2026-11-19T10:00:00+00:00', WARSAW),
    ('2026-11-19T11:00-08:00', 'PST', '2026-11-19T19:00:00+00:00', 'UTC-08:00'),
    # Last instants before/after the EU switch (29 March 2026, 01:00 UTC).
    ('2026-03-29T01:59', WARSAW, '2026-03-29T00:59:00+00:00', WARSAW),
    ('2026-03-29T03:00', WARSAW, '2026-03-29T01:00:00+00:00', WARSAW),
])
def test_parse_when_converts_explicit_zones(value, stated, utc, label):
    when, got_label, problem = parse_when(value, fallback_tz=WARSAW, stated_tz=stated)
    assert problem is None
    assert when.isoformat() == utc and got_label == label


@pytest.mark.parametrize('value,stated,problem', [
    ('2026-11-19T11:00', 'PST', 'unsupported_timezone'),       # an abbreviation, not an offset or IANA name
    ('2026-11-19T11:00', 'CEST', 'unsupported_timezone'),
    ('2026-11-19T11:00', 'UTC+15', 'unsupported_timezone'),    # out of range
    ('2026-07-01T11:00+01:00', WARSAW, 'conflicting_timezone'),  # Warsaw is +02:00 in July
    ('2026-03-29T02:30', WARSAW, 'nonexistent_time'),           # skipped by spring-forward
    ('2026-10-25T02:30', WARSAW, 'ambiguous_time'),             # repeated by fall-back
    ('2026-03-08T02:30', 'America/New_York', 'nonexistent_time'),
])
def test_parse_when_refuses_to_guess(value, stated, problem):
    assert parse_when(value, fallback_tz=WARSAW, stated_tz=stated) == (None, None, problem)


def test_parse_when_rejects_invalid_values():
    assert parse_when('next Tuesday', fallback_tz=WARSAW, stated_tz=None) == (None, None, None)
    assert parse_when('1999-01-01T10:00', fallback_tz=WARSAW, stated_tz=None) == (None, None, None)
    assert parse_when(None, fallback_tz=WARSAW, stated_tz='UTC+2') == (None, None, None)


def interview_with_time(w, marker, *, scheduled_at, timezone):
    a = imported_app(w, f'{marker}-C')
    quote = 'We would like to invite you to an interview'
    email(w, marker, f'{quote} for Junior Data Analyst at Acme on 19 November at 11:00 {timezone}.',
          item(kind='existing_application_update', event_type='interview_invitation',
               application_evidence='active_process', supporting_quote=quote, scheduled_at=scheduled_at,
               timezone=timezone, proposed_status='Interview'))
    sync_now(w)
    return a


def test_unconvertible_interview_time_goes_to_review_and_the_user_sets_it(world):
    w = world
    a = interview_with_time(w, 'TZ-PST', scheduled_at='2026-11-19T11:00', timezone='PST')
    [interview] = [e for e in events(w) if e.event_type == 'interview_invitation']
    assert interview.scheduled_at is None and interview.scheduled_tz is None  # never a confidently wrong time
    [r] = [r for r in reviews(w) if r.kind == 'schedule_change']
    assert r.proposal['timeIssue'] == 'unsupported_timezone'
    assert r.proposal['statedTime'] == '2026-11-19T11:00 PST' and r.proposal['scheduledAt'] is None
    assert approve(w, str(r.id)).status_code == 422  # a time is required
    assert approve(w, str(r.id), scheduledAt='2026-11-19T11:00').status_code == 422  # offset required
    assert approve(w, str(r.id), scheduledAt='2026-11-19T11:00:00-08:00').status_code == 200
    detail = w['client'].get(f'{BASE}/applications/{a.id}', headers=w['headers']).json()
    [act] = [e for e in detail['activity'] if e['eventType'] == 'interview_invitation']
    assert act['scheduledAt'] == '2026-11-19T19:00:00+00:00' and act['scheduledTz'] is None and act['corrected']


def test_fixed_offset_interview_time_is_converted_without_review(world):
    w = world
    interview_with_time(w, 'TZ-UTC2', scheduled_at='2026-11-19T11:00', timezone='UTC+2')
    [interview] = [e for e in events(w) if e.event_type == 'interview_invitation']
    assert interview.scheduled_at.replace(tzinfo=UTC).isoformat() == '2026-11-19T09:00:00+00:00'
    assert interview.scheduled_tz == 'UTC+02:00'
    assert not [r for r in reviews(w) if r.kind == 'schedule_change']


def test_correcting_an_activity_time_drops_the_email_zone_label(world):
    w = world
    a = interview_with_time(w, 'TZ-CET', scheduled_at='2026-11-19T11:00', timezone='CET')
    [interview] = [e for e in events(w) if e.event_type == 'interview_invitation']
    assert interview.scheduled_tz == 'CET'
    r = w['client'].patch(f'{BASE}/applications/{a.id}/activity/{interview.id}', headers=w['headers'],
                          json={'scheduledAt': '2026-11-20T12:00:00+01:00'})
    assert r.status_code == 200
    [act] = [e for e in r.json()['activity'] if e['eventType'] == 'interview_invitation']
    assert act['scheduledAt'] == '2026-11-20T11:00:00+00:00' and act['scheduledTz'] is None


# =================================================================================================================
# Small release items
# =================================================================================================================

def _access_record(path):
    return logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ('127.0.0.1:5000', 'GET', path, '1.1', 303), None)


def test_oauth_callback_query_is_redacted_from_access_logs():
    f = RedactOAuthCallback()
    record = _access_record('/api/v1/gmail/oauth/callback?state=SECRET-STATE&code=SECRET-CODE')
    assert f.filter(record) is True
    message = record.getMessage()
    assert 'SECRET' not in message and '/api/v1/gmail/oauth/callback?[redacted]' in message
    other = _access_record('/api/v1/applications?sort=date')
    f.filter(other)
    assert other.getMessage().endswith('"GET /api/v1/applications?sort=date HTTP/1.1" 303')


def test_callback_logs_never_contain_the_code(client, fake_google, caplog):
    """End to end through the app: the redaction filter is installed on uvicorn's access logger."""
    from app.gmail.oauth import install_log_redaction
    install_log_redaction()
    access = logging.getLogger('uvicorn.access')
    assert any(isinstance(f, RedactOAuthCallback) for f in access.filters)
    headers = signup(client)
    params, cookie = start_flow(client, headers)
    record = _access_record(f'/api/v1/gmail/oauth/callback?state={params["state"]}&code=auth-code')
    for f in access.filters:
        f.filter(record)
    assert params['state'] not in record.getMessage() and 'auth-code' not in record.getMessage()
