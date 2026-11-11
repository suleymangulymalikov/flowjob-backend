"""AI-first matching prompt contract and second-person wording (no OpenAI calls)."""
import re

from beanie import PydanticObjectId

from app.ai_matching.matching import SYSTEM_PROMPT
from app.matches.models import MatchRecord
from app.services import ai_matching
from tests.test_matches import BASE, JOB, make_cv, mock_ai, signup  # noqa: F401  (mock_ai is a fixture)

THIRD_PERSON = re.compile(r"\b(the candidate|the applicant|candidate's|applicant's)\b", re.I)
ABSENCE = re.compile(r"\b(you lack|you do not have|you don't have|you are missing|you have no)\b", re.I)


def test_prompt_is_the_approved_ai_first_prompt_and_keeps_scoring_out_of_the_model():
    p = SYSTEM_PROMPT
    assert ai_matching.SYSTEM_PROMPT is p  # the service uses the AI-first matching prompt verbatim
    for phrase in ('untrusted DATA', 'NEVER working conditions', 'never extra requirements',
                   'NOT evidence just because it is authentic', 'never guess, never invent',
                   'Not shown in your CV.', 'Do NOT output a numeric score',
                   'rejects any requirement whose quote it cannot find'):
        assert phrase in p, phrase


def test_mock_analysis_speaks_to_the_user_and_never_claims_a_skill_is_absent():
    a = ai_matching._mock_result().analysis
    texts = [r.explanation for r in a.requirements]
    for text in texts:
        assert not THIRD_PERSON.search(text) and not ABSENCE.search(text), text
    unshown = [r for r in a.requirements if r.status == 'not_shown']
    assert unshown and all(r.explanation == 'Not shown in your CV.' and r.evidence == [] for r in unshown)


def test_new_reports_record_the_ai_first_prompt_version(client, mock_ai):
    h = signup(client)
    m = client.post(f'{BASE}/matches', headers=h,
                    json={'cvId': make_cv(client, h), 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}).json()
    assert ai_matching.PROMPT_VERSION == 'ai-first-exp-1'
    assert m['score'] == 80 and m['analysisVersion'] == 'ai-first-1'

    async def stored():
        return (await MatchRecord.get(PydanticObjectId(m['id']))).prompt_version

    assert client.portal.call(stored) == 'ai-first-exp-1'


def test_historical_third_person_report_is_untouched_and_not_reused(client, mock_ai):
    h = signup(client)
    body = {'cvId': make_cv(client, h), 'jobTitle': 'Backend Engineer', 'jobDescription': JOB}
    first = client.post(f'{BASE}/matches', headers=h, json=body).json()
    old_text = 'The candidate has strong Python experience but lacks Docker.'

    async def make_historical():
        await MatchRecord.get_pymongo_collection().update_one(
            {'_id': PydanticObjectId(first['id'])},
            {'$set': {'prompt_version': '2', 'result.overallAssessment': old_text, 'result.summary': old_text}},
        )

    client.portal.call(make_historical)
    historical = client.get(f'{BASE}/matches/{first["id"]}', headers=h).json()
    assert historical['overallAssessment'] == old_text  # legacy wording is shown exactly as stored
    second = client.post(f'{BASE}/matches', headers=h, json=body).json()
    assert second['id'] != first['id'] and mock_ai['calls'] == 2  # an old prompt's cache entry is never reused
    assert client.get(f'{BASE}/matches/{first["id"]}', headers=h).json() == historical
