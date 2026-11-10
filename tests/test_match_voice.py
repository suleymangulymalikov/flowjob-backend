"""Second-person ("Your CV shows ...") wording for AI job-match reports.

OpenAI is never called: the prompt is inspected as text, the canned OPENAI_API_KEY=mock analysis is
checked directly, and the provider is replaced by a fake for the API tests. Historical records are
never rewritten.
"""
import re

from beanie import PydanticObjectId

from app.matches.models import MatchRecord
from app.matches.schemas import ANALYSIS_VERSION, PROMPT_VERSION
from app.services import ai_matching
from tests.test_matches import BASE, JOB, make_cv, mock_ai, signup  # noqa: F401  (mock_ai is a fixture)

THIRD_PERSON = re.compile(r'\b(the candidate|the applicant|candidate\'s|applicant\'s)\b', re.I)
ABSENCE_CLAIMS = re.compile(r'\b(you lack|you do not have|you don\'t have|you are missing|you have no)\b', re.I)


def _texts(analysis):
    out = [analysis.overall_assessment]
    out += [r.evidence for r in analysis.requirements]
    out += analysis.strengths + analysis.areas_to_improve + analysis.recommendations
    out += [w.suggestion for w in analysis.suggested_wording] + analysis.clarifying_questions
    return out


def test_system_prompt_requires_second_person_and_forbids_third_person():
    p = ai_matching.SYSTEM_PROMPT
    assert 'second person' in p and '"Your CV shows' in p
    assert 'Never write "the candidate"' in p
    assert 'two or three sentences' in p and 'single biggest gap' in p


def test_system_prompt_never_implies_absence_from_a_missing_mention():
    p = ai_matching.SYSTEM_PROMPT
    assert 'Never imply a skill is absent just because the CV does not mention it' in p
    assert '"not shown in your CV"' in p and '"Not shown in your CV."' in p
    assert 'unless the CV explicitly says so' in p


def test_system_prompt_limits_improvements_to_evidence_supported_ones():
    p = ai_matching.SYSTEM_PROMPT
    assert 'at most three concrete changes' in p
    assert 'Return an empty list when the CV does not support' in p
    assert 'never pad the list' in p


def test_prompt_still_treats_cv_and_job_as_data_and_keeps_scoring_out_of_the_model():
    p = ai_matching.SYSTEM_PROMPT
    assert 'untrusted DATA' in p and 'Do NOT output a numeric score' in p
    assert '<job_description>' in ai_matching.build_input('Dev', None, 'job text', 'cv text')


def test_mock_analysis_speaks_to_the_user_and_never_claims_a_skill_is_absent():
    a = ai_matching._mock_result().analysis
    assert a.overall_assessment.startswith('Your CV')
    for text in _texts(a):
        assert not THIRD_PERSON.search(text), text
        assert not ABSENCE_CLAIMS.search(text), text
    unshown = [r for r in a.requirements if r.status.value == 'not_mentioned']
    assert unshown and all(r.evidence == 'Not shown in your CV.' for r in unshown)
    assert 1 <= len(a.recommendations) <= 3


def test_new_reports_record_the_new_prompt_version_and_keep_the_scoring_rubric(client, mock_ai):
    h = signup(client)
    m = client.post(f'{BASE}/matches', headers=h,
                    json={'cvId': make_cv(client, h), 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}).json()
    assert PROMPT_VERSION == '2' and ANALYSIS_VERSION == '1'
    assert m['score'] == 80  # scoring untouched by the wording change

    async def stored():
        return (await MatchRecord.get(PydanticObjectId(m['id']))).prompt_version

    assert client.portal.call(stored) == PROMPT_VERSION


def test_historical_third_person_report_is_untouched_and_not_reused(client, mock_ai):
    h = signup(client)
    body = {'cvId': make_cv(client, h), 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}
    first = client.post(f'{BASE}/matches', headers=h, json=body).json()
    old_text = 'The candidate has strong Python experience but lacks Docker.'

    async def make_historical():
        await MatchRecord.get_pymongo_collection().update_one(
            {'_id': PydanticObjectId(first['id'])},
            {'$set': {'prompt_version': '1', 'result.overallAssessment': old_text, 'result.summary': old_text}},
        )

    client.portal.call(make_historical)
    historical = client.get(f'{BASE}/matches/{first["id"]}', headers=h).json()
    assert historical['overallAssessment'] == old_text  # legacy wording is shown exactly as stored
    second = client.post(f'{BASE}/matches', headers=h, json=body).json()
    assert second['id'] != first['id'] and mock_ai['calls'] == 2  # a new request never reuses the old wording
    assert client.get(f'{BASE}/matches/{first["id"]}', headers=h).json() == historical  # stored record unchanged
    assert client.get(f'{BASE}/matches/{second["id"]}', headers=h).json()['overallAssessment'] != old_text
