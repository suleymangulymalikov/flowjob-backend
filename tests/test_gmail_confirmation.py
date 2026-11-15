"""Explicit application confirmations, model confidence and match-key handling (the WhyTech case). All mocked."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.gmail.classify import GMAIL_PROMPT_VERSION, SYSTEM_PROMPT, EmailAnalysis, resolve_key, validate
from app.gmail.models import GmailSuppression
from app.gmail.preprocess import Minimized
from tests.gmail_fakes import BASE, connect, fake_google, signup  # noqa: F401
from tests.test_gmail_pipeline import (  # noqa: F401
    FakeAI, ai, api_apps, apps, approve, email, events, item, manual_app, reviews, run, sync_now, world,
)

WHYTECH_QUOTE = 'We are pleased to confirm that we have received your job application, submitted on 14 November 2026.'
WHYTECH_BODY = (
    'Dear Candidate,\n\n' + WHYTECH_QUOTE + '\nPosition: Junior Software Developer\n'
    'Reference: WT-JSD-2026-014\n\nOur recruitment team will review your profile and contact you.\n\n'
    'Kind regards,\nWhyTech sp. z o.o. Recruitment'
)
WHYTECH_SENDER = 'WhyTech Recruitment <careers@whytech.example>'


def whytech(**kw):
    base = dict(employer='WhyTech sp. z o.o.', job_title='Junior Software Developer', references=['WT-JSD-2026-014'],
                supporting_quote=WHYTECH_QUOTE, application_evidence='confirmation',
                event_type='application_confirmation', kind='submitted_application', confidence='medium')
    base.update(kw)
    return item(**base)


def whytech_email(w, marker='WT', **kw):
    return email(w, marker, WHYTECH_BODY, whytech(**kw), subject=f'Application received {marker}',
                 sender=WHYTECH_SENDER)


def only_review(w):
    [r] = reviews(w)
    return r


# --- the reported case -------------------------------------------------------------------------------------------

def test_whytech_confirmation_at_medium_confidence_is_imported(world):
    w = world
    manual_app(w, 'RemoExperts', 'Remote Data Annotator Jobs in Warsaw')  # an unrelated tracked application
    mid = whytech_email(w, match_key=None)
    sync_now(w)
    imported = [a for a in api_apps(w) if a['company'] == 'WhyTech sp. z o.o.']
    assert len(imported) == 1 and reviews(w) == []
    a = imported[0]
    sent = datetime.fromtimestamp(int(w['google'].messages[mid]['internalDate']) / 1000, UTC)
    assert (a['jobTitle'], a['status'], a['cvId'], a['source']) == ('Junior Software Developer', 'Applied', None, 'gmail')
    assert a['dateApplied'] == sent.astimezone(ZoneInfo('Europe/Warsaw')).date().isoformat()
    assert a['dateAppliedProvisional'] is False
    assert a['activity'][0]['excerpt'] == WHYTECH_QUOTE


@pytest.mark.parametrize('placeholder', ['none', 'None', 'null', 'N/A', 'n/a', '', '-'])
def test_no_match_placeholders_do_not_block_the_import(world, placeholder):
    w = world
    manual_app(w, 'RemoExperts', 'Remote Data Annotator Jobs in Warsaw')
    whytech_email(w, f'PH{abs(hash(placeholder))}', match_key=placeholder, candidate_keys=[placeholder])
    sync_now(w)
    assert [a.company for a in apps(w)].count('WhyTech sp. z o.o.') == 1 and reviews(w) == []


def test_high_confidence_explicit_confirmation_is_imported(world):
    w = world
    whytech_email(w, confidence='high')
    sync_now(w)
    assert [a.company for a in apps(w)] == ['WhyTech sp. z o.o.']


# --- invalid keys and confidence ---------------------------------------------------------------------------------

@pytest.mark.parametrize('key', ['A7', 'RemoExperts', 'app1'])
def test_unvalidated_application_keys_go_to_review(world, key):
    w = world
    manual_app(w, 'RemoExperts', 'Remote Data Annotator Jobs in Warsaw')
    whytech_email(w, f'K{key}', match_key=key, confidence='high')
    sync_now(w)
    assert [a.company for a in apps(w)] == ['RemoExperts']
    r = only_review(w)
    assert r.proposal['reason'] == 'invalid_match' and r.proposal['signals']['invalidMatchKey'] is True


def test_low_confidence_confirmation_goes_to_review(world):
    w = world
    whytech_email(w, confidence='low')
    sync_now(w)
    assert apps(w) == [] and only_review(w).proposal['reason'] == 'low_confidence'


@pytest.mark.parametrize('change', [
    {'event_type': 'interview_invitation', 'application_evidence': 'active_process'},  # inferred, not a receipt
    {'application_evidence': 'reference'},
    {'kind': 'existing_application_update'},
    {'event_type': 'application_update'},
])
def test_medium_confidence_needs_an_explicit_receipt(world, change):
    w = world
    whytech_email(w, f'M{sorted(change.items())}', **change)
    sync_now(w)
    assert apps(w) == [] and only_review(w).proposal['reason'] == 'medium_confidence'


def test_medium_confidence_needs_the_title_itself_in_the_email(world):
    w = world
    body = 'Dear Candidate,\n' + WHYTECH_QUOTE + '\nReference: WT-JSD-2026-014\nWhyTech sp. z o.o.'
    email(w, 'NOTITLE', body, whytech(), subject='Application received NOTITLE', sender=WHYTECH_SENDER)
    sync_now(w)
    assert apps(w) == [] and only_review(w).proposal['reason'] == 'medium_confidence'


# --- advertisements, invitations, ungrounded identity ------------------------------------------------------------

def test_invitation_to_apply_never_imports(world):
    w = world
    email(w, 'INV', 'WhyTech sp. z o.o. is hiring a Junior Software Developer. Apply now on our careers page!',
          whytech(kind='invitation_to_apply', application_evidence='none',
                  supporting_quote='is hiring a Junior Software Developer. Apply now'),
          sender=WHYTECH_SENDER)
    sync_now(w)
    assert apps(w) == [] and reviews(w) == []


def test_advertisement_mislabelled_as_confirmation_goes_to_review(world):
    w = world
    email(w, 'ADV2', 'WhyTech sp. z o.o.: Junior Software Developer. Apply now and we will review your application!',
          whytech(supporting_quote='Apply now and we will review your application!'), sender=WHYTECH_SENDER)
    sync_now(w)
    assert apps(w) == [] and only_review(w).proposal['reason'] == 'possible_advertisement'


def test_interest_without_an_application_at_medium_confidence_goes_to_review(world):
    w = world
    email(w, 'INT', 'Thanks for your interest in the Junior Software Developer role at WhyTech sp. z o.o.',
          whytech(supporting_quote='Thanks for your interest in the Junior Software Developer role'),
          sender=WHYTECH_SENDER)
    sync_now(w)
    r = only_review(w)
    assert apps(w) == [] and r.proposal['reason'] == 'medium_confidence'
    assert r.proposal['signals']['quoteAboutApplication'] is False


def test_invented_employer_goes_to_review(world):
    w = world
    whytech_email(w, employer='Google')
    sync_now(w)
    assert apps(w) == [] and only_review(w).proposal['reason'] == 'ungrounded_employer'


def test_invented_title_without_reference_goes_to_review(world):
    w = world
    whytech_email(w, job_title='Senior Cloud Architect', references=[])
    sync_now(w)
    assert apps(w) == [] and only_review(w).proposal['reason'] == 'ungrounded_title'


# --- multilingual ------------------------------------------------------------------------------------------------

def test_polish_confirmation_at_medium_confidence(world):
    w = world
    quote = 'Potwierdzamy otrzymanie Twojej aplikacji na stanowisko Młodszy Programista'
    email(w, 'PL', f'Dzień dobry,\n{quote} w firmie Kowalski S.A.\nPozdrawiamy',
          whytech(employer='Kowalski S.A.', job_title='Młodszy Programista', references=[], supporting_quote=quote),
          subject='Potwierdzenie PL', sender='Rekrutacja <hr@kowalski.example>')
    sync_now(w)
    assert [(a.company, a.job_title) for a in apps(w)] == [('Kowalski S.A.', 'Młodszy Programista')]


def test_german_confirmation_at_medium_confidence(world):
    w = world
    quote = 'wir bestätigen den Eingang Ihrer Bewerbung als Werkstudent Softwareentwicklung'
    email(w, 'DE', f'Sehr geehrte Damen und Herren,\n{quote} bei der Muster GmbH.',
          whytech(employer='Muster GmbH', job_title='Werkstudent Softwareentwicklung', references=[],
                  supporting_quote=quote),
          subject='Eingangsbestätigung DE', sender='Muster Karriere <jobs@muster.example>')
    sync_now(w)
    assert [(a.company, a.job_title) for a in apps(w)] == [('Muster GmbH', 'Werkstudent Softwareentwicklung')]


# --- duplicates, deletions, existing applications ----------------------------------------------------------------

def test_second_confirmation_links_instead_of_duplicating(world):
    w = world
    whytech_email(w, 'D1')
    sync_now(w)
    whytech_email(w, 'D2', match_key='A1')
    sync_now(w)
    assert [a.company for a in apps(w)] == ['WhyTech sp. z o.o.'] and len(events(w)) == 2


def test_deleted_medium_confidence_import_is_not_recreated(world):
    w = world
    first = whytech_email(w, 'DEL1')
    sync_now(w)
    [a] = apps(w)
    assert w['client'].delete(f'{BASE}/applications/{a.id}', headers=w['headers']).status_code == 204
    assert run(w['client'], GmailSuppression.find_all().count()) == 1
    email(w, 'DEL2', WHYTECH_BODY, whytech(), subject='Application received DEL2', sender=WHYTECH_SENDER,
          thread=w['google'].messages[first]['threadId'])
    # The same reference is the same application's evidence: suppressed, not even reviewed.
    whytech_email(w, 'DEL3')
    # A new thread without that reference might be a genuine reapplication: the user decides.
    body = 'Dear Candidate,\n' + WHYTECH_QUOTE + '\nPosition: Junior Software Developer\nWhyTech sp. z o.o.'
    email(w, 'DEL4', body, whytech(references=[]), subject='Application received DEL4', sender=WHYTECH_SENDER)
    sync_now(w)
    assert apps(w) == []
    assert [r.proposal['reason'] for r in reviews(w)] == ['previously_deleted']


def test_existing_application_status_change_still_needs_approval(world):
    w = world
    app_id = manual_app(w, 'WhyTech sp. z o.o.', 'Junior Software Developer', status='Saved')
    whytech_email(w, 'EX', match_key='A1')
    sync_now(w)
    a = w['client'].get(f'{BASE}/applications/{app_id}', headers=w['headers']).json()
    assert a['status'] == 'Saved' and a['suggestion']['proposedStatus'] == 'Applied'
    assert len(apps(w)) == 1
    assert approve(w, a['suggestion']['id']).status_code == 200
    assert w['client'].get(f'{BASE}/applications/{app_id}', headers=w['headers']).json()['status'] == 'Applied'


# --- diagnostics and contract ------------------------------------------------------------------------------------

def test_review_signals_are_minimal_enums_and_booleans(world):
    w = world
    whytech_email(w, confidence='low')
    sync_now(w)
    s = only_review(w).proposal['signals']
    assert set(s) == {'kind', 'eventType', 'evidence', 'confidence', 'quoteVerified', 'quoteStrong',
                      'employerGrounded', 'titleGrounded', 'referenceVerified', 'invalidMatchKey', 'callToApply',
                      'contradictions', 'quoteAboutApplication'}
    assert all(isinstance(v, bool) or v in {'submitted_application', 'application_confirmation', 'confirmation', 'low'}
               for v in s.values())
    assert s['confidence'] == 'low' and s['quoteVerified'] and s['employerGrounded'] and s['titleGrounded']


def test_resolve_key_unit():
    keys = {'A1': 'app-1', 'A2': 'app-2'}
    assert resolve_key('A1', keys) == ('app-1', False)
    assert resolve_key(' a2 ', keys) == ('app-2', False)
    for placeholder in (None, '', 'none', 'NULL', 'N/A', '-', 'no match'):
        assert resolve_key(placeholder, keys) == (None, False)
    for invalid in ('A3', 'A0', 'B1', 'Acme', '5'):
        assert resolve_key(invalid, keys) == (None, True)
    mini = Minimized(text=WHYTECH_BODY, subject='Application received', sender_name='WhyTech Recruitment',
                     sender_domain='whytech.example')
    out = validate(EmailAnalysis(is_recruitment=True, items=[whytech(match_key='none')]), mini, [], 'Europe/Warsaw')
    [i] = out['items']
    assert not i['alias_invalid'] and i['quote_strong'] and i['quote_application'] and i['employer_grounded']


def test_prompt_is_versioned_and_asks_for_null_and_calibrated_confidence():
    assert GMAIL_PROMPT_VERSION == 'gmail-3'
    assert 'return null (JSON' in SYSTEM_PROMPT and 'is\n  normally high' in SYSTEM_PROMPT
