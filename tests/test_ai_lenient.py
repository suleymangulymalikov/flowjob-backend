"""An AI reply that slightly overshoots a length limit is fitted, not rejected (it used to fail with HTTP 502)."""
import logging

from pydantic import ValidationError

from app.ai_matching.matching import MatchAnalysis
from app.services import ai_provider
from app.tailor.schemas import MAX_CHANGES, TailoringAnalysis


def _change(**over):
    return {'op': 'rewrite', 'path': 'summary', 'suggested': 'Backend developer building Python APIs.',
            'reason': 'Leads with the backend work this job asks for.', **over}


def test_tailoring_reply_over_the_limits_is_fitted():
    reply = TailoringAnalysis.model_validate({
        'changes': [_change(reason='word ' * 80, job_requirement='r' * 300) for _ in range(MAX_CHANGES + 3)],
        'missing_qualifications': ['Kubernetes'] * 20, 'clarifying_questions': ['?'] * 9,
    })
    assert len(reply.changes) == MAX_CHANGES
    assert len(reply.missing_qualifications) == 12 and len(reply.clarifying_questions) == 8
    first = reply.changes[0]
    assert len(first.reason) <= 240 and first.reason.endswith('…') and len(first.job_requirement) <= 160


def test_an_over_long_rewrite_is_dropped_never_cut_off():
    reply = TailoringAnalysis.model_validate({'changes': [_change(suggested='x ' * 500), _change()]})
    assert reply.changes[0].suggested is None  # grounding then rejects it as empty_suggestion
    assert reply.changes[1].suggested == 'Backend developer building Python APIs.'


def test_match_reply_over_the_limits_is_fitted_and_quotes_stay_prefixes():
    quote = 'Built REST APIs in Python and FastAPI for a logistics platform ' * 6
    requirement = {'id': 'r1', 'requirement': 'Python', 'category': 'required', 'source_quote': 'Python',
                   'status': 'demonstrated', 'evidence': [{'path': 'summary', 'quote': quote}] * 5,
                   'explanation': 'Shown in your summary.'}
    reply = MatchAnalysis.model_validate({'enough_info': True, 'requirements': [requirement] * 30, 'excluded': []})
    assert len(reply.requirements) == 25 and len(reply.requirements[0].evidence) == 3
    cited = reply.requirements[0].evidence[0].quote
    assert len(cited) <= 240 and quote.startswith(cited.removesuffix('…'))


def test_validation_failures_log_the_fields_not_the_text(caplog):
    try:
        MatchAnalysis.model_validate({'enough_info': 'secret CV text', 'requirements': []})
    except ValidationError as error:
        with caplog.at_level(logging.WARNING):
            ai_provider._log_failure(error)
    line = caplog.records[-1].getMessage()
    assert 'fields=enough_info:bool_parsing' in line and 'secret CV text' not in line
