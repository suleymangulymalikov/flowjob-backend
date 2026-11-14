"""The authoritative backend policy that turns frozen, validated email items into tracker changes.

The model's output is a suggestion. This module decides, using deterministic evidence:

* New applications are created automatically only with strong, verified evidence that the user actually applied
  (a confirmation, a verified application reference, or an active recruitment step for an identified position),
  a verified supporting quote, an identifiable employer and title, high confidence, no contradictions, no
  duplicate risk and no suppression. Everything else goes to Needs Review.
* Existing applications are linked by thread, verified reference, or a model choice that agrees with the
  employer/title evidence and is clearly differentiated; otherwise the user chooses.
* Existing statuses never change without approval: only the latest applicable suggestion stays pending, older
  ones are superseded, and emails older than the user's last status decision produce none.
* Every effect is idempotent (unique keys per message item), so retries, restarts and crashes cannot duplicate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from beanie import PydanticObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.applications.models import Application, ApplicationEvent, ApplicationStatus
from app.gmail.classify import employer_similar, norm_name, norm_ref, signals, title_similarity
from app.gmail.connection import as_utc, now, purge_if_disconnected
from app.gmail.models import GmailConnection, GmailMessage, GmailReview, GmailSuppression

NON_APPLICATION_KINDS = {'job_advertisement', 'invitation_to_apply', 'newsletter', 'other'}
INFERRING_EVENTS = {'assessment', 'interview_invitation', 'interview_rescheduled', 'offer', 'rejection'}
STATUS_FROM_EVENT = {
    'application_confirmation': 'Applied', 'interview_invitation': 'Interview', 'interview_rescheduled': 'Interview',
    'offer': 'Offer', 'rejection': 'Rejected', 'withdrawal_confirmation': 'Withdrawn',
}
RANK = {'Saved': 0, 'Applied': 1, 'Interview': 2, 'Offer': 3}
TERMINAL = {'Rejected', 'Withdrawn'}
INTERVIEW_FAMILY = {'interview_invitation', 'interview_rescheduled'}
DEADLINE_FAMILY = {'assessment', 'deadline_extension'}
TITLE_MATCH = 0.6


@dataclass
class Effects:
    imported: int = 0
    linked: int = 0
    activities: int = 0
    reviews: int = 0
    notes: list[str] = field(default_factory=list)


def status_value(app: Application) -> str:
    return app.status.value if isinstance(app.status, ApplicationStatus) else str(app.status)


def local_date(at: datetime, tz: str):
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo('UTC')
    return as_utc(at).astimezone(zone).date()


def advances(current: str, target: str) -> bool:
    if current in TERMINAL:
        return False
    if target in TERMINAL:
        return True
    return RANK.get(target, -1) > RANK.get(current, 99)


def family(event_type: str) -> set[str] | None:
    if event_type in INTERVIEW_FAMILY:
        return INTERVIEW_FAMILY
    if event_type in DEADLINE_FAMILY:
        return DEADLINE_FAMILY
    return None


# --- context ---------------------------------------------------------------------------------------------------

@dataclass
class UserState:
    apps: list[Application]
    thread_apps: dict[str, set[str]]
    refs: dict[str, set[str]]  # application id -> normalized references
    suppressions: list[GmailSuppression]


async def load_state(conn: GmailConnection) -> UserState:
    apps = await Application.find(Application.user_id == conn.user_id, ignore_cache=True).to_list()
    thread_apps: dict[str, set[str]] = {}
    refs: dict[str, set[str]] = {}
    for a in apps:
        if a.gmail_thread_id:
            thread_apps.setdefault(a.gmail_thread_id, set()).add(str(a.id))
    async for e in ApplicationEvent.get_pymongo_collection().find(
            {'user_id': conn.user_id, 'source': 'gmail', 'state': 'active'},
            {'application_id': 1, 'thread_id': 1, 'references': 1}):
        if e.get('thread_id'):
            thread_apps.setdefault(e['thread_id'], set()).add(str(e['application_id']))
        for r in e.get('references') or []:
            refs.setdefault(str(e['application_id']), set()).add(norm_ref(r))
    suppressions = await GmailSuppression.find(GmailSuppression.connection_id == conn.id).to_list()
    return UserState(apps=apps, thread_apps=thread_apps, refs=refs, suppressions=suppressions)


def shortlist(state: UserState, thread_id: str | None, text: str, limit: int = 20) -> list[dict[str, Any]]:
    """User-scoped candidates shown to the model, keyed A1..An (it never sees database ids)."""
    haystack = norm_name(text)

    def score(a: Application) -> int:
        s = 0
        if thread_id and str(a.id) in state.thread_apps.get(thread_id, set()):
            s += 100
        company = norm_name(a.company)
        if company and company in haystack:
            s += 10
        if any(t in haystack for t in norm_name(a.job_title).split() if len(t) > 3):
            s += 2
        return s
    ranked = sorted(state.apps, key=lambda a: (-score(a), -(as_utc(a.updated_at) or now()).timestamp()))
    chosen = ranked if len(state.apps) <= limit else [a for a in ranked if score(a) > 0][:limit]
    return [{
        'key': f'A{i + 1}', 'application_id': str(a.id), 'company': a.company[:120], 'job_title': a.job_title[:120],
        'status': status_value(a), 'date': a.date_applied.isoformat() if a.date_applied else None,
        'references': sorted(state.refs.get(str(a.id), set()))[:3],
    } for i, a in enumerate(chosen[:limit])]


# --- matching --------------------------------------------------------------------------------------------------

@dataclass
class Resolution:
    kind: str  # linked | ambiguous | none
    app: Application | None = None
    via: str | None = None  # thread | reference | model | exact
    candidates: list[Application] = field(default_factory=list)


def resolve(item: dict[str, Any], msg: GmailMessage, state: UserState) -> Resolution:
    """Link only on independent evidence; the model's choice alone never links.

    Employer identity: the item's employer when the email states one, otherwise the application's employer must be
    named in the email itself. A matching title alone never identifies an application.
    """
    by_id = {str(a.id): a for a in state.apps}
    mentioned = set(item.get('mentioned_app_ids') or [])

    def employer_ok(a: Application) -> bool:
        if item['employer']:
            return employer_similar(a.company, item['employer'])
        return str(a.id) in mentioned

    def title_ok(a: Application) -> bool:
        return not item['job_title'] or title_similarity(a.job_title, item['job_title']) >= TITLE_MATCH

    # 1. A verified reference, but only within a compatible employer (identical IDs recur across companies).
    item_refs = {norm_ref(r) for r in item['references']}
    ref_hits = [by_id[aid] for aid, refs in state.refs.items() if item_refs & refs and aid in by_id]
    ref_ok = [a for a in ref_hits if employer_ok(a)]
    if len(ref_ok) == 1:
        return Resolution('linked', ref_ok[0], 'reference')

    # 2. The same Gmail thread is strong identity evidence; stated employer/title must not contradict it.
    thread_hits = [by_id[aid] for aid in state.thread_apps.get(msg.thread_id or '', set()) if aid in by_id]
    thread_ok = [a for a in thread_hits if title_ok(a)
                 and (not item['employer'] or employer_similar(a.company, item['employer']))]
    if len(thread_ok) == 1 and not ref_ok:
        return Resolution('linked', thread_ok[0], 'thread')

    # 3. Employer-confirmed candidates. Without a stated title, only a single application at that employer counts.
    similar = [a for a in state.apps if employer_ok(a) and title_ok(a)]
    # Reference hits that cannot be confirmed (the email names no employer) stay as candidates for the user;
    # hits whose employer contradicts the email are discarded.
    unconfirmed_refs = ref_hits if not item['employer'] else ref_ok
    pool = {str(a.id): a for a in [*similar, *unconfirmed_refs, *thread_ok,
                                   *(by_id[i] for i in item['candidate_ids'] if i in by_id)]}
    chosen = by_id.get(item['match_id'] or '')
    if chosen is not None and chosen in similar:
        best = title_similarity(chosen.job_title, item['job_title']) if item['job_title'] else 0.0
        rivals = [a for a in similar if a.id != chosen.id
                  and (not item['job_title'] or title_similarity(a.job_title, item['job_title']) >= best)]
        if not rivals:
            return Resolution('linked', chosen, 'model')
    if chosen is not None:
        pool[str(chosen.id)] = chosen
    if chosen is None and item['employer'] and item['job_title']:
        exact = [a for a in similar if norm_name(a.job_title) == norm_name(item['job_title'])]
        if len(exact) == 1 and len(similar) == 1:
            return Resolution('linked', exact[0], 'exact')
    if pool:
        return Resolution('ambiguous', candidates=list(pool.values()))
    return Resolution('none')


def suppression_for(item: dict[str, Any], msg: GmailMessage, state: UserState) -> tuple[str | None, list]:
    """'same' = evidence of a deleted application (never recreate); 'similar' = maybe a new application."""
    refs = {norm_ref(r) for r in item['references']}
    similar: list[GmailSuppression] = []
    for s in state.suppressions:
        if msg.message_id in s.message_ids or (msg.thread_id and msg.thread_id in s.thread_ids) \
                or refs & {norm_ref(r) for r in s.references}:
            return 'same', [s]
        if item['employer'] and s.employer_norm and employer_similar(s.employer_norm, item['employer']) \
                and (not item['job_title'] or title_similarity(s.title_norm, item['job_title']) >= TITLE_MATCH):
            similar.append(s)
    return ('similar', similar) if similar else (None, [])


def auto_import_ok(item: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether an unmatched item may create an application by itself. Returns (ok, reason).

    Every reason names the first failed check, so a review says why it was not imported. Grounding and safety checks
    apply at every confidence level; confidence is judged last.
    """
    if item['kind'] not in ('submitted_application', 'existing_application_update'):
        return False, 'not_an_application'
    if not item['employer'] or not item['job_title']:
        return False, 'missing_details'
    if item['contradictions']:
        return False, 'contradictory'
    if item['alias_invalid']:
        return False, 'invalid_match'
    if not item.get('quote_ok'):
        return False, 'unverified_quote'
    if not item.get('quote_strong'):
        return False, 'weak_quote'
    # Grounding: the stated employer and position must come from the email, not only from the model.
    if not item.get('employer_grounded'):
        return False, 'ungrounded_employer'
    if not (item.get('title_grounded') or item['references']):
        return False, 'ungrounded_title'
    if item.get('call_to_apply'):
        return False, 'possible_advertisement'
    evidence, event = item['evidence'], item['event_type']
    if item['confidence'] == 'low':
        return False, 'low_confidence'
    if item['confidence'] == 'medium':
        # Only an explicit receipt/confirmation of a submitted application may import at medium confidence: the
        # model must label it so consistently (kind, evidence and event), the job title itself must be grounded
        # (a reference alone is not enough here) and the verified excerpt must itself be about an application.
        # Inferred or contextual applications (interviews, rejections, references) still need high confidence.
        explicit = (item['kind'] == 'submitted_application' and evidence == 'confirmation'
                    and event == 'application_confirmation' and item.get('title_grounded')
                    and item.get('quote_application'))
        return (True, 'confirmation') if explicit else (False, 'medium_confidence')
    if evidence == 'confirmation' and event in ('application_confirmation', 'application_update'):
        return True, 'confirmation'
    if evidence == 'reference' and item['references']:
        return True, 'reference'
    if evidence in ('active_process', 'confirmation', 'reference') and event in INFERRING_EVENTS:
        return True, 'active_process'
    return False, 'weak_evidence'


def possible_reapplication(item: dict[str, Any], app: Application, via: str, state: UserState) -> bool:
    if item['kind'] != 'submitted_application' or via in ('thread', 'reference'):
        return False
    if status_value(app) in TERMINAL:
        return True
    known = state.refs.get(str(app.id), set())
    mine = {norm_ref(r) for r in item['references']}
    return bool(known and mine and not known & mine)


# --- writes ----------------------------------------------------------------------------------------------------

def source_key(conn: GmailConnection, msg: GmailMessage, item: dict[str, Any]) -> str:
    return f'{conn.id}:{msg.message_id}:{item["key"]}'


async def upsert_event(conn: GmailConnection, msg: GmailMessage, item: dict[str, Any], app_id, *,
                       scheduled: tuple[str | None, str | None, str | None] | None = None) -> ApplicationEvent:
    when, tz, before = scheduled if scheduled is not None else (item['scheduled_at'], item['scheduled_tz'],
                                                                item['previous_scheduled_at'])
    event = ApplicationEvent(
        user_id=conn.user_id, application_id=PydanticObjectId(app_id), source='gmail',
        event_type=item['event_type'] if item['event_type'] != 'other' else 'application_update',
        occurred_at=msg.internal_at, connection_id=conn.id, message_id=msg.message_id, thread_id=msg.thread_id,
        item_key=item['key'], excerpt=item['quote'], references=item['references'],
        scheduled_at=datetime.fromisoformat(when) if when else None, scheduled_tz=tz if when else None,
        previous_scheduled_at=datetime.fromisoformat(before) if before else None,
    )
    try:
        await event.insert()
    except DuplicateKeyError:
        # Already recorded (retry/restart). Never overwrite: the user may have corrected or dismissed it.
        existing = await ApplicationEvent.find_one(
            ApplicationEvent.user_id == conn.user_id, ApplicationEvent.message_id == msg.message_id,
            ApplicationEvent.item_key == item['key'], ignore_cache=True)
        assert existing is not None
        return existing
    # The user may have deleted the application meanwhile: never leave orphan activity.
    if not await Application.find_one(Application.id == event.application_id, ignore_cache=True).exists():
        await event.delete()
    return event


async def add_review(conn: GmailConnection, msg: GmailMessage, item: dict[str, Any], kind: str, *,
                     application_id=None, candidates: list[Application] | None = None,
                     source_event_id=None, proposal: dict[str, Any] | None = None) -> GmailReview | None:
    review = GmailReview(
        user_id=conn.user_id, connection_id=conn.id, message_id=msg.message_id, thread_id=msg.thread_id,
        item_key=item['key'], kind=kind, application_id=PydanticObjectId(application_id) if application_id else None,
        candidate_ids=[c.id for c in candidates or []], source_event_id=source_event_id,
        proposal=proposal or {}, excerpt=item['quote'], internal_at=msg.internal_at,
    )
    try:
        await review.insert()
    except DuplicateKeyError:
        return None  # already proposed (and possibly dismissed) for this message item
    return review


def application_proposal(conn: GmailConnection, msg: GmailMessage, item: dict[str, Any], reason: str) -> dict:
    target = item['proposed_status'] or STATUS_FROM_EVENT.get(item['event_type'])
    return {
        'company': item['employer'], 'jobTitle': item['job_title'],
        'status': target if target and target != 'Saved' else 'Applied',
        'dateApplied': local_date(msg.internal_at, conn.timezone).isoformat(),
        'dateProvisional': item['event_type'] != 'application_confirmation',
        'eventType': item['event_type'], 'references': item['references'], 'reason': reason,
        'scheduledAt': item['scheduled_at'],
        'signals': signals(item),  # enums/booleans only, for diagnosis; never email text or model output
    }


async def propose_status(conn: GmailConnection, msg: GmailMessage, item: dict[str, Any], app: Application,
                         event: ApplicationEvent, effects: Effects) -> None:
    target = item['proposed_status'] or STATUS_FROM_EVENT.get(item['event_type'])
    current = status_value(app)
    if not target or target == current or not advances(current, target):
        return
    basis = as_utc(app.status_basis_at)
    if basis and as_utc(msg.internal_at) <= basis:
        return  # older than the user's latest status decision
    if event.state != 'active':
        return
    review = await add_review(conn, msg, item, 'status_change', application_id=app.id, source_event_id=event.id,
                              proposal={'from': current, 'to': target, 'eventType': item['event_type']})
    if review is None:
        return
    effects.reviews += 1
    await attach_suggestion(app.id, review)


async def attach_suggestion(app_id, review: GmailReview) -> None:
    """Keep only the newest applicable pending suggestion on the application; mark the other one superseded."""
    apps = Application.get_pymongo_collection()
    reviews = GmailReview.get_pymongo_collection()
    for _ in range(5):
        doc = await apps.find_one({'_id': app_id}, {'suggestion_review_id': 1, 'status_basis_at': 1})
        if doc is None:
            await reviews.update_one({'_id': review.id, 'state': 'pending'}, {'$set': {'state': 'invalidated'}})
            return
        current_id = doc.get('suggestion_review_id')
        if current_id:
            current = await reviews.find_one({'_id': current_id}, {'internal_at': 1, 'state': 1})
            if current and current.get('state') == 'pending' and \
                    as_utc(current['internal_at']) >= as_utc(review.internal_at):
                await reviews.update_one({'_id': review.id, 'state': 'pending'},
                                         {'$set': {'state': 'superseded', 'decided_at': now()}})
                return
        swapped = await apps.update_one({'_id': app_id, 'suggestion_review_id': current_id},
                                        {'$set': {'suggestion_review_id': review.id}})
        if swapped.matched_count == 1:
            if current_id:
                await reviews.update_one({'_id': current_id, 'state': 'pending'},
                                         {'$set': {'state': 'superseded', 'decided_at': now()}})
            return


async def schedule_plan(app: Application, msg: GmailMessage, item: dict[str, Any]) -> tuple[tuple, str | None]:
    """Return ((scheduled_at, tz, previous) for the event, previous date for a review or None if automatic)."""
    when = item['scheduled_at']
    fam = family(item['event_type'])
    moving = item['event_type'] in ('interview_rescheduled', 'deadline_extension') or item['schedule_change'] != 'none'
    if not when or not fam or not moving:
        return (when, item['scheduled_tz'], item['previous_scheduled_at'] if when else None), None
    latest = await ApplicationEvent.find(
        ApplicationEvent.application_id == app.id, ApplicationEvent.state == 'active',
        {'event_type': {'$in': list(fam)}, 'scheduled_at': {'$ne': None}, 'occurred_at': {'$lt': msg.internal_at}},
        ignore_cache=True,
    ).sort('-occurred_at').first_or_none()
    previous = as_utc(latest.scheduled_at).isoformat() if latest and latest.scheduled_at else \
        item['previous_scheduled_at']
    clear = item['schedule_change'] == 'clear' and item['quote_ok'] and item['confidence'] != 'low'
    if not clear or (latest and latest.corrected):
        # Ambiguous, or it would silently override a date the user corrected: the user decides.
        return (None, None, None), previous or ''
    return (when, item['scheduled_tz'], previous), None


async def apply_item(conn: GmailConnection, msg: GmailMessage, item: dict[str, Any], state: UserState,
                     effects: Effects) -> None:
    if item['kind'] in NON_APPLICATION_KINDS:
        return
    suppressed, sup = suppression_for(item, msg, state)
    res = resolve(item, msg, state)

    if res.kind == 'ambiguous':
        proposal = application_proposal(conn, msg, item, 'ambiguous_match')
        if await add_review(conn, msg, item, 'ambiguous_link', candidates=res.candidates, proposal=proposal):
            effects.reviews += 1
        return

    if res.kind == 'linked':
        app = res.app
        if possible_reapplication(item, app, res.via or '', state):
            proposal = application_proposal(conn, msg, item, 'possible_reapplication')
            if await add_review(conn, msg, item, 'new_application', candidates=[app], proposal=proposal):
                effects.reviews += 1
            return
        plan, review_previous = await schedule_plan(app, msg, item)
        event = await upsert_event(conn, msg, item, app.id, scheduled=plan)
        effects.activities += 1
        effects.linked += 1
        if review_previous is not None:
            proposal = {'scheduledAt': item['scheduled_at'], 'scheduledTz': item['scheduled_tz'],
                        'previousScheduledAt': review_previous or None, 'eventType': item['event_type']}
            if await add_review(conn, msg, item, 'schedule_change', application_id=app.id,
                                source_event_id=event.id, proposal=proposal):
                effects.reviews += 1
        await propose_status(conn, msg, item, app, event, effects)
        return

    # No existing application.
    if suppressed == 'same':
        effects.notes.append('suppressed')
        return  # evidence of an application the user deleted: never recreate it
    ok, reason = auto_import_ok(item)
    if suppressed == 'similar':
        ok, reason = False, 'previously_deleted'
    if not ok:
        proposal = application_proposal(conn, msg, item, reason)
        if await add_review(conn, msg, item, 'new_application', proposal=proposal):
            effects.reviews += 1
        return
    app = await import_application(conn, msg, item, provisional=reason != 'confirmation'
                                   or item['event_type'] != 'application_confirmation')  # date of a receipt
    state.apps.append(app)
    if msg.thread_id:
        state.thread_apps.setdefault(msg.thread_id, set()).add(str(app.id))
    state.refs.setdefault(str(app.id), set()).update(norm_ref(r) for r in item['references'])
    effects.imported += 1
    event = await upsert_event(conn, msg, item, app.id)
    effects.activities += 1
    await propose_status(conn, msg, item, app, event, effects)


async def import_application(conn: GmailConnection, msg: GmailMessage, item: dict[str, Any], *,
                             provisional: bool) -> Application:
    key = source_key(conn, msg, item)
    app = Application(
        user_id=conn.user_id, company=item['employer'][:200], job_title=item['job_title'][:200],
        status=ApplicationStatus.applied, date_applied=local_date(msg.internal_at, conn.timezone), cv_id=None,
        source='gmail', imported_at=now(), date_applied_provisional=provisional, gmail_connection_id=conn.id,
        gmail_source_key=key, gmail_message_id=msg.message_id, gmail_thread_id=msg.thread_id,
    )
    try:
        await app.insert()
    except DuplicateKeyError:
        existing = await Application.find_one(Application.user_id == conn.user_id,
                                              Application.gmail_source_key == key, ignore_cache=True)
        if existing is None:
            raise
        return existing
    return app


async def apply_analysis(conn: GmailConnection, msg: GmailMessage, analysis: dict[str, Any], fence) -> Effects:
    effects = Effects()
    state = await load_state(conn)
    for item in analysis.get('items') or []:
        await fence()
        await apply_item(conn, msg, item, state, effects)
    return effects


# --- shared with the review and application endpoints ----------------------------------------------------------

async def invalidate_for_event(user_id, event_id) -> None:
    """A corrected or dismissed event can no longer justify its pending status/schedule suggestion."""
    reviews = GmailReview.get_pymongo_collection()
    async for r in reviews.find({'user_id': user_id, 'source_event_id': event_id, 'state': 'pending'},
                                {'_id': 1, 'application_id': 1}):
        await reviews.update_one({'_id': r['_id'], 'state': 'pending'},
                                 {'$set': {'state': 'invalidated', 'decided_at': now()}})
        await Application.get_pymongo_collection().update_one(
            {'_id': r.get('application_id'), 'suggestion_review_id': r['_id']},
            {'$unset': {'suggestion_review_id': ''}})


async def invalidate_status_suggestion(app: Application) -> None:
    """A manual status decision makes the pending Gmail suggestion obsolete."""
    doc = await Application.get_pymongo_collection().find_one_and_update(
        {'_id': app.id, 'suggestion_review_id': {'$ne': None}}, {'$unset': {'suggestion_review_id': ''}},
        projection={'suggestion_review_id': 1}, return_document=ReturnDocument.BEFORE)
    if doc and doc.get('suggestion_review_id'):
        await GmailReview.get_pymongo_collection().update_one(
            {'_id': doc['suggestion_review_id'], 'state': 'pending'},
            {'$set': {'state': 'invalidated', 'decided_at': now()}})


async def on_application_deleted(app: Application) -> None:
    """Remember a deleted Gmail-linked application's evidence while connected; drop its activity and reviews."""
    events = await ApplicationEvent.find(ApplicationEvent.application_id == app.id, ignore_cache=True).to_list()
    conn = await GmailConnection.find_one(GmailConnection.user_id == app.user_id, ignore_cache=True)
    gmail_events = [e for e in events if e.source == 'gmail']
    if conn and conn.status != 'disconnecting' and (app.gmail_connection_id == conn.id or gmail_events):
        await GmailSuppression(
            user_id=app.user_id, connection_id=conn.id,
            message_ids=sorted({m for m in [app.gmail_message_id, *(e.message_id for e in gmail_events)] if m}),
            thread_ids=sorted({t for t in [app.gmail_thread_id, *(e.thread_id for e in gmail_events)] if t}),
            references=sorted({r for e in gmail_events for r in e.references}),
            employer_norm=norm_name(app.company), title_norm=norm_name(app.job_title),
        ).insert()
        await purge_if_disconnected(app.user_id, conn.id)  # a suppression must not outlive a racing disconnect
    await ApplicationEvent.get_pymongo_collection().delete_many({'application_id': app.id, 'user_id': app.user_id})
    await GmailReview.get_pymongo_collection().update_many(
        {'user_id': app.user_id, 'application_id': app.id, 'state': 'pending'},
        {'$set': {'state': 'invalidated', 'decided_at': now()}})
