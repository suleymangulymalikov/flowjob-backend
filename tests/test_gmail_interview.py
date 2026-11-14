"""Interview invitations for existing applications, the recovery safety net for inconsistent model labels, and
the sanitized ledger decision (the WhyTech Junior Data Analyst case). All mocked; no network."""
import pytest

from app.gmail import reconcile
from app.gmail.models import GmailMessage
from tests.gmail_fakes import BASE, connect, fake_google, signup  # noqa: F401
from tests.test_gmail_pipeline import (  # noqa: F401
    FakeAI, ai, api_apps, apps, approve, email, events, item, manual_app, reviews, run, sync_now, world,
)

EMPLOYER = 'WhyTech sp. z o.o.'
TITLE = 'Junior Data Analyst'
REF = 'WT-JDA-2026-015'
SENDER = 'WhyTech Recruitment <careers@whytech.example>'
CONFIRM_QUOTE = 'We are pleased to confirm that we have received your job application for Junior Data Analyst'
INVITE_QUOTE = 'We would like to invite you to an online interview on 19 November 2026 at 11:00 CET.'
INVITE_BODY = (
    f'Dear Candidate,\nThank you for your application for the {TITLE} position (reference {REF}).\n'
    f'{INVITE_QUOTE}\nPlease confirm your availability.\nKind regards,\n{EMPLOYER} Recruitment'
)


def jda_application(w):
    """The existing application, imported from its confirmation (so its reference is known)."""
    email(w, 'JDA-CONF', f'{CONFIRM_QUOTE} at {EMPLOYER}. Reference: {REF}.',
          item(employer=EMPLOYER, job_title=TITLE, references=[REF], supporting_quote=CONFIRM_QUOTE),
          subject='Application received JDA-CONF', sender=SENDER, minutes_ago=120)
    sync_now(w)
    [a] = [a for a in api_apps(w) if a['jobTitle'] == TITLE]
    return a['id']


def invitation(**kw):
    base = dict(kind='existing_application_update', event_type='interview_invitation', employer=EMPLOYER,
                job_title=TITLE, references=[REF], application_evidence='active_process', confidence='high',
                supporting_quote=INVITE_QUOTE, proposed_status='Interview', scheduled_at='2026-11-19T11:00',
                timezone='CET', match_key=None)
    base.update(kw)
    return item(**base)


def send_invitation(w, marker, *, body=INVITE_BODY, is_recruitment=True, **kw):
    return email(w, marker, body, invitation(**kw), subject=f'Interview {marker}', sender=SENDER,
                 is_recruitment=is_recruitment, minutes_ago=30)


def decision_of(w, mid):
    return run(w['client'], GmailMessage.find_one(GmailMessage.message_id == mid)).decision


def detail(w, app_id):
    return w['client'].get(f'{BASE}/applications/{app_id}', headers=w['headers']).json()


def assert_interview_recorded(w, app_id):
    a = detail(w, app_id)
    assert a['status'] == 'Applied'  # never changed without approval
    interview = [e for e in a['activity'] if e['eventType'] == 'interview_invitation']
    assert len(interview) == 1
    assert interview[0]['scheduledAt'] == '2026-11-19T10:00:00+00:00'  # 11:00 CET
    assert interview[0]['excerpt'] == INVITE_QUOTE
    assert a['suggestion']['proposedStatus'] == 'Interview' and a['suggestion']['currentStatus'] == 'Applied'
    return a


# --- correctly and incorrectly labelled invitations -------------------------------------------------------------

def test_whytech_interview_invitation_correctly_classified(world):
    w = world
    app_id = jda_application(w)
    mid = send_invitation(w, 'OK')
    sync_now(w)
    assert_interview_recorded(w, app_id)
    d = decision_of(w, mid)
    assert d['isRecruitment'] is True and d['itemCount'] == 1
    assert d['items'][0] == {'kind': 'existing_application_update', 'eventType': 'interview_invitation',
                             'confidence': 'high', 'evidence': 'active_process', 'action': 'linked',
                             'reason': 'reference'}


def test_invitation_mislabelled_as_invitation_to_apply_is_recovered(world):
    w = world
    app_id = jda_application(w)
    mid = send_invitation(w, 'MISKIND', kind='invitation_to_apply', application_evidence='none')
    sync_now(w)
    assert_interview_recorded(w, app_id)
    assert decision_of(w, mid)['items'][0]['action'] == 'recovered_linked'
    assert len(apps(w)) == 1  # the recovery path never creates applications


def test_invitation_with_is_recruitment_false_is_recovered(world):
    w = world
    app_id = jda_application(w)
    mid = send_invitation(w, 'NOTREC', is_recruitment=False)
    sync_now(w)
    assert_interview_recorded(w, app_id)
    d = decision_of(w, mid)
    assert d['isRecruitment'] is False and d['items'][0]['action'] == 'recovered_linked'


def test_status_stays_applied_until_the_user_approves(world):
    w = world
    app_id = jda_application(w)
    send_invitation(w, 'APPROVE', kind='other')
    sync_now(w)
    a = assert_interview_recorded(w, app_id)
    assert approve(w, a['suggestion']['id']).status_code == 200
    assert detail(w, app_id)['status'] == 'Interview'


# --- things that must stay ignored or unrecovered ----------------------------------------------------------------

def test_genuine_invitation_to_apply_is_ignored(world):
    w = world
    app_id = jda_application(w)
    quote = "We think you'd be a great fit for our Junior Data Analyst opening. Apply here"
    mid = email(w, 'ITA', f'{quote} on the {EMPLOYER} careers page.',
                item(kind='invitation_to_apply', event_type='other', employer=EMPLOYER, job_title=TITLE,
                     application_evidence='none', supporting_quote=quote),
                subject='Opening ITA', sender=SENDER)
    sync_now(w)
    assert [e for e in detail(w, app_id)['activity'] if e['eventType'] != 'application_confirmation'] == []
    assert reviews(w) == [] and len(apps(w)) == 1
    assert decision_of(w, mid)['items'][0]['action'] == 'ignored_non_application'


def test_advertisement_wording_blocks_recovery(world):
    w = world
    app_id = jda_application(w)
    quote = 'Apply now and get an interview for Junior Data Analyst within 48 hours!'
    mid = email(w, 'ADV', f'{EMPLOYER}: {quote}',
                invitation(kind='job_advertisement', supporting_quote=quote, references=[]),
                subject='Hiring ADV', sender=SENDER)
    sync_now(w)
    assert detail(w, app_id)['suggestion'] is None
    entry = decision_of(w, mid)['items'][0]
    assert (entry['action'], entry['reason']) == ('not_recovered', 'advertisement_wording')


def test_excerpt_that_does_not_support_the_event_blocks_recovery(world):
    w = world
    app_id = jda_application(w)
    quote = f'Thank you for your application for the {TITLE} position'
    mid = send_invitation(w, 'WRONGEV', kind='invitation_to_apply', supporting_quote=quote)
    sync_now(w)
    assert detail(w, app_id)['suggestion'] is None and len(detail(w, app_id)['activity']) == 1
    assert decision_of(w, mid)['items'][0]['reason'] == 'event_not_supported_by_quote'


def test_unrelated_employer_is_not_recovered(world):
    w = world
    app_id = jda_application(w)
    body = f'Dear Candidate,\n{INVITE_QUOTE}\nPosition: {TITLE}\nBeta Analytics Recruitment'
    mid = email(w, 'BETA', body, invitation(kind='invitation_to_apply', employer='Beta Analytics', references=[]),
                subject='Interview BETA', sender='Beta <jobs@beta.example>')
    sync_now(w)
    assert len(detail(w, app_id)['activity']) == 1 and detail(w, app_id)['suggestion'] is None
    assert decision_of(w, mid)['items'][0]['reason'] == 'no_verified_match'


def test_shared_reference_links_only_the_compatible_employer(world):
    w = world
    app_id = jda_application(w)
    email(w, 'BETA-CONF', f'Thank you for applying to Beta Analytics for Data Intern. Reference: {REF}.',
          item(employer='Beta Analytics', job_title='Data Intern', references=[REF],
               supporting_quote='Thank you for applying to Beta Analytics for Data Intern'),
          subject='Application received BETA-CONF', sender='Beta <jobs@beta.example>', minutes_ago=60)
    sync_now(w)
    beta = [a['id'] for a in api_apps(w) if a['company'] == 'Beta Analytics']
    assert len(beta) == 1
    send_invitation(w, 'SHARED', kind='invitation_to_apply')
    sync_now(w)
    assert_interview_recorded(w, app_id)
    assert detail(w, beta[0])['suggestion'] is None and len(detail(w, beta[0])['activity']) == 1


def test_ambiguous_match_is_not_recovered(world):
    w = world
    manual_app(w, EMPLOYER, TITLE)
    manual_app(w, EMPLOYER, 'Junior Data Analyst Intern')
    mid = send_invitation(w, 'AMB', kind='invitation_to_apply', references=[],
                          body=f'Dear Candidate,\n{INVITE_QUOTE}\nPosition: {TITLE}\n{EMPLOYER}')
    sync_now(w)
    assert events(w) == [] and reviews(w) == []  # recovery never creates reviews or events when unsure
    assert decision_of(w, mid)['items'][0]['reason'] == 'ambiguous_match'


def test_unique_exact_employer_and_title_is_recovered_without_reference(world):
    w = world
    app_id = manual_app(w, EMPLOYER, TITLE)
    mid = send_invitation(w, 'EXACT', kind='invitation_to_apply', references=[],
                          body=f'Dear Candidate,\n{INVITE_QUOTE}\nPosition: {TITLE}\n{EMPLOYER}')
    sync_now(w)
    assert_interview_recorded(w, app_id)
    assert decision_of(w, mid)['items'][0]['reason'] == 'exact'


@pytest.mark.parametrize('key,reason', [('A9', 'invalid_match'), ('RemoExperts', 'invalid_match')])
def test_invalid_application_reference_is_not_recovered(world, key, reason):
    w = world
    app_id = jda_application(w)
    mid = send_invitation(w, f'KEY{key}', kind='invitation_to_apply', match_key=key)
    sync_now(w)
    assert detail(w, app_id)['suggestion'] is None
    assert decision_of(w, mid)['items'][0]['reason'] == reason


def test_model_only_match_is_not_recovered(world):
    w = world
    app_id = manual_app(w, EMPLOYER, TITLE)
    mid = send_invitation(w, 'MODEL', kind='invitation_to_apply', references=[], employer=None, job_title=None,
                          match_key='A1', body=f'Dear Candidate,\n{INVITE_QUOTE}\n{EMPLOYER}')
    sync_now(w)
    assert detail(w, app_id)['activity'] == []
    assert decision_of(w, mid)['items'][0]['reason'] == 'model_only_match'


def test_ordinary_non_recruitment_mail_stays_ignored(world):
    w = world
    mid = email(w, 'NEWS', 'Our quarterly newsletter: product news and webinars.',
                item(kind='newsletter', event_type='other', application_evidence='none',
                     supporting_quote='Our quarterly newsletter: product news'),
                is_recruitment=False, sender=SENDER)
    sync_now(w)
    assert apps(w) == [] and events(w) == []
    assert decision_of(w, mid)['items'][0]['action'] == 'ignored_not_recruitment'


# --- duplicates, protections, diagnostics ------------------------------------------------------------------------

def test_repeated_processing_does_not_duplicate_activity(world):
    w = world
    app_id = jda_application(w)
    mid = send_invitation(w, 'REPEAT', kind='invitation_to_apply')
    sync_now(w)
    sync_now(w)  # already processed: not analysed again
    msg = run(w['client'], GmailMessage.find_one(GmailMessage.message_id == mid))
    frozen = {'is_recruitment': True, 'items': [reconcile_item(w, mid)]}

    async def nothing():
        return None
    run(w['client'], reconcile.apply_analysis(w['conn'], msg, frozen, nothing))  # e.g. resumed after a crash
    a = assert_interview_recorded(w, app_id)
    assert len(reviews(w)) == 1 and a['suggestion']['id'] == str(reviews(w)[0].id)


def reconcile_item(w, mid):
    """Rebuild the frozen item exactly as validation produced it (keys are content-derived and stable)."""
    from app.gmail.classify import EmailAnalysis, validate
    from app.gmail.preprocess import Minimized
    mini = Minimized(text=INVITE_BODY, subject='Interview REPEAT', sender_name='WhyTech Recruitment',
                     sender_domain='whytech.example')
    out = validate(EmailAnalysis(is_recruitment=True, items=[invitation(kind='invitation_to_apply')]), mini, [],
                   'Europe/Warsaw')
    return out['items'][0]


def test_dismissed_interview_activity_is_not_recreated(world):
    w = world
    app_id = jda_application(w)
    mid = send_invitation(w, 'DISMISS', kind='invitation_to_apply')
    sync_now(w)
    event_id = [e['id'] for e in detail(w, app_id)['activity'] if e['eventType'] == 'interview_invitation'][0]
    w['client'].post(f'{BASE}/applications/{app_id}/activity/{event_id}/dismiss', headers=w['headers'])
    msg = run(w['client'], GmailMessage.find_one(GmailMessage.message_id == mid))

    async def nothing():
        return None
    run(w['client'], reconcile.apply_analysis(w['conn'], msg, {'is_recruitment': True,
                                                               'items': [reconcile_item(w, mid)]}, nothing))
    a = detail(w, app_id)
    assert [e['eventType'] for e in a['activity']] == ['application_confirmation'] and a['suggestion'] is None


ALLOWED_ACTIONS = {'linked', 'review', 'imported', 'suppressed', 'recovered_linked', 'not_recovered',
                   'ignored_non_application', 'ignored_not_recruitment'}


def test_ledger_decision_is_sanitized(world):
    w = world
    jda_application(w)
    mid = send_invitation(w, 'SAN', kind='invitation_to_apply')
    sync_now(w)
    d = decision_of(w, mid)
    assert set(d) == {'isRecruitment', 'itemCount', 'items', 'promptVersion'}
    assert isinstance(d['isRecruitment'], bool) and isinstance(d['itemCount'], int) and d['promptVersion'] == 'gmail-3'
    for entry in d['items']:
        assert set(entry) == {'kind', 'eventType', 'confidence', 'evidence', 'action', 'reason'}
        assert entry['action'] in ALLOWED_ACTIONS
        for value in entry.values():
            assert value is None or (isinstance(value, str) and len(value) <= 40 and ' ' not in value)
    blob = str(d)
    for secret in (INVITE_QUOTE, 'Dear Candidate', REF, EMPLOYER, TITLE, 'whytech.example'):
        assert secret not in blob
    msg = run(w['client'], GmailMessage.find_one(GmailMessage.message_id == mid))
    assert msg.analysis is None  # the frozen model output is still cleared after processing


def test_older_ledger_rows_and_frozen_analyses_stay_compatible(world):
    w = world
    app_id = jda_application(w)
    # A row processed before the decision field existed loads fine and the status endpoint works.
    run(w['client'], GmailMessage.get_pymongo_collection().update_many({}, {'$unset': {'decision': ''}}))
    assert all(m.decision is None for m in run(w['client'], GmailMessage.find_all().to_list()))
    assert w['client'].get(f'{BASE}/gmail', headers=w['headers']).status_code == 200
    # A frozen analysis from an older version (no quote_event signal) is never recovered by accident.
    mid = send_invitation(w, 'OLD', kind='invitation_to_apply')
    sync_now(w)  # processed with the current version
    msg = run(w['client'], GmailMessage.find_one(GmailMessage.message_id == mid))
    legacy = {k: v for k, v in reconcile_item(w, mid).items() if k != 'quote_event'}
    legacy['key'] = 'legacy-key'
    state = run(w['client'], reconcile.load_state(w['conn']))
    action = run(w['client'], reconcile.apply_item(w['conn'], msg, legacy, state, reconcile.Effects(),
                                                   is_recruitment=True))
    assert action == ('not_recovered', 'event_not_supported_by_quote')
    assert len([e for e in detail(w, app_id)['activity'] if e['eventType'] == 'interview_invitation']) == 1
