"""AI-first Job Matching experiment: deterministic tests with synthetic fixtures and mocked AI responses.

No network, no paid calls, no database writes: `evaluate` is pure. The "AI responses" below are hand-written
`ExperimentAnalysis` objects — both a well-behaved model following the finalized rules on a RemoExperts-style
post, and misbehaving ones, to show exactly what this minimal backend does and does not catch.
"""
import copy
from fractions import Fraction

import pytest

from app.experiment.matching import (
    MIN_QUALIFICATIONS, NOT_SHOWN_TEXT, SHARES, SYSTEM_PROMPT, Citation, ExcludedItem, ExperimentAnalysis,
    Requirement, build_input, evaluate, render_cv, valid_citation, verdict_for,
)

# --- synthetic fixtures (fictional person, fictional company) ---------------------------------------------

CV = {
    'fullName': 'Sam Example', 'email': 'sam@example.test', 'phone': '+1 555 0100', 'location': 'Springfield',
    'links': ['example.test/sam'],
    'summary': 'Computer Science student with hands-on experience in data annotation quality review.',
    'experience': [
        {'id': 'exp-1', 'title': 'Data Annotation Intern', 'company': 'Fictional Labs', 'start': '2024-06', 'end': '2024-09',
         'bullets': [
             'Reviewed 300+ annotated text samples per week against the project labelling guidelines.',
             'Wrote and updated a shared documentation page describing common labelling errors.',
         ]},
    ],
    'education': [
        {'id': 'edu-1', 'school': 'Example University', 'degree': 'BSc Computer Science (in progress)',
         'note': 'Coursework: Statistics, Databases'},
    ],
    'projects': [
        {'id': 'proj-1', 'name': 'Sales Insights Dashboard', 'tech': 'Power BI, SQL',
         'description': 'Built a Power BI dashboard on a SQL sales database.'},
        {'id': 'proj-2', 'name': 'FitTrack', 'tech': 'React Native',
         'description': 'Built a cross-platform mobile app that tracks workouts.'},
    ],
    'skills': ['Python', 'SQL', 'Excel'],
    'languages': 'English (fluent)',
}

JOB = """About the Job
Lumen Annotation Co. helps AI teams improve training data quality. This is a remote, full-time contract role.

What You Will Do
- Review annotated text samples and give feedback to annotators.
- Build simple dashboards to track annotation quality.

Requirements
- Experience performing quality assurance on annotated or labelled data.
- Strong written documentation skills.
- Experience building data pipelines with Python and SQL.
- Proficiency in Python or R for data analysis.
- Hands-on experience evaluating LLM outputs or RLHF workflows.
- Comfortable working remotely with a distributed team.
- Passionate about data quality.
- Familiarity with annotation tools such as Label Studio (preferred).

Nice to Have
- Experience building dashboards in Power BI.
"""


def R(id, text, category, status, evidence=(), explanation=None, **kw):
    return Requirement(id=id, requirement=text, category=category, status=status, evidence=list(evidence),
                       explanation=explanation or (NOT_SHOWN_TEXT if status == 'not_shown' else f'Your CV shows {text}.'),
                       **kw)


def good_model_response():
    """What a model following the finalized rules returns for JOB + CV."""
    return ExperimentAnalysis(
        enough_info=True,
        overall_note='Your CV clearly shows annotation QA and documentation work; LLM evaluation is the biggest gap.',
        requirements=[
            R('r1', 'QA on annotated data', 'required', 'demonstrated',
              [Citation(path='experience.exp-1.bullets.0', quote='Reviewed 300+ annotated text samples per week')],
              'Your internship reviewed hundreds of annotated samples weekly against guidelines.'),
            R('r2', 'Written documentation skills', 'required', 'demonstrated',
              [Citation(path='experience.exp-1.bullets.1', quote='Wrote and updated a shared documentation page')]),
            R('r3', 'Data pipelines with Python and SQL', 'required', 'partial',
              [Citation(path='skills', quote='Python, SQL')],
              'You list Python and SQL, but no pipeline work is shown — a meaningful component only.'),
            R('r4', 'Python or R for data analysis', 'required', 'demonstrated',
              [Citation(path='skills', quote='Python')]),
            R('r5', 'Evaluating LLM outputs or RLHF workflows', 'required', 'not_shown'),
            R('r6', 'Annotation tools such as Label Studio', 'preferred', 'not_shown'),
            R('r7', 'Dashboards in Power BI', 'preferred', 'demonstrated',
              [Citation(path='projects.proj-1.description', quote='Built a Power BI dashboard')]),
        ],
        excluded=[
            ExcludedItem(text='Comfortable working remotely with a distributed team', reason='working_condition'),
            ExcludedItem(text='Passionate about data quality', reason='personality_trait'),
            ExcludedItem(text='Build simple dashboards to track annotation quality', reason='job_duty'),
        ],
    )


# --- the happy path: RemoExperts-style scenario -----------------------------------------------------------

def test_remoexperts_scenario_scores_the_approved_way():
    result = evaluate(good_model_response(), CV)
    # Required: 3 demonstrated + 1 partial + 1 not_shown of 5 -> 7/10. Preferred: 1 of 2 -> 1/2. Standard empty.
    # 100 * (60*(7/10) + 15*(1/2)) / 75 = 66
    assert result['score'] == 66 and result['verdict'] == 'Good fit'
    assert result['qualificationCounts']['required'] == {'demonstrated': 3, 'partial': 1, 'not_shown': 1, 'count': 5}
    assert result['qualificationCounts']['standard']['count'] == 0
    assert result['diagnostics'] == {'citations_dropped': 0, 'statuses_downgraded': 0, 'duplicate_ids': 0}


def test_working_conditions_duties_and_traits_are_outside_the_scored_set():
    result = evaluate(good_model_response(), CV)
    scored = {r['requirement'] for r in result['requirements']}
    assert not scored & {'Comfortable working remotely with a distributed team', 'Passionate about data quality',
                         'Build simple dashboards to track annotation quality'}
    assert {e['reason'] for e in result['excluded']} == {'working_condition', 'personality_trait', 'job_duty'}


def test_llm_evaluation_is_not_shown_with_the_neutral_sentence_and_no_evidence():
    llm = next(r for r in evaluate(good_model_response(), CV)['requirements'] if r['id'] == 'r5')
    assert llm['status'] == 'not_shown' and llm['evidence'] == [] and llm['explanation'] == NOT_SHOWN_TEXT


def test_combined_and_alternative_requirements_are_single_entries():
    result = evaluate(good_model_response(), CV)
    assert sum('Python' in r['requirement'] for r in result['requirements']) == 2  # one combined, one alternatives


# --- what the minimal backend catches ---------------------------------------------------------------------

def mutate(edit):
    analysis = good_model_response()
    edit(analysis)
    return analysis


def test_a_fabricated_quote_is_dropped_and_the_claim_earns_nothing():
    analysis = mutate(lambda a: a.requirements.__setitem__(4, R(
        'r5', 'Evaluating LLM outputs or RLHF workflows', 'required', 'demonstrated',
        [Citation(path='experience.exp-1.bullets.0', quote='evaluated LLM outputs for RLHF')],
        'Your internship evaluated LLM outputs.')))
    result = evaluate(analysis, CV)
    llm = next(r for r in result['requirements'] if r['id'] == 'r5')
    assert llm['status'] == 'not_shown' and llm['explanation'] == NOT_SHOWN_TEXT
    assert result['score'] == 66  # identical to the honest answer
    assert result['diagnostics'] == {'citations_dropped': 1, 'statuses_downgraded': 1, 'duplicate_ids': 0}


@pytest.mark.parametrize('path, quote', [
    ('experience.exp-9.bullets.0', 'Reviewed 300+ annotated text samples'),   # no such role
    ('experience.exp-1.bullets.1', 'Reviewed 300+ annotated text samples'),   # real text, wrong item
    ('projects.proj-1.description', 'annotated text samples'),                # cross-item
    ('skills', 'Label Studio'),                                               # not in the list
    ('summary', ''),                                                          # empty quote
    ('links.0', 'example.test/sam'),                                          # PII never citable
])
def test_invalid_or_cross_item_citations_never_validate(path, quote):
    assert not valid_citation(CV, Citation(path=path, quote=quote))


def test_case_and_whitespace_fold_but_paraphrase_does_not():
    assert valid_citation(CV, Citation(path='experience.exp-1.bullets.0', quote='reviewed  300+ ANNOTATED text samples'))
    assert not valid_citation(CV, Citation(path='experience.exp-1.bullets.0', quote='Checked 300 annotation samples'))


def test_duplicate_requirement_ids_count_once():
    analysis = mutate(lambda a: a.requirements.append(R(
        'r7', 'Dashboards in Power BI', 'preferred', 'demonstrated',
        [Citation(path='projects.proj-1.tech', quote='Power BI')])))
    result = evaluate(analysis, CV)
    assert result['qualificationCounts']['preferred']['count'] == 2 and result['diagnostics']['duplicate_ids'] == 1


def test_malformed_output_is_rejected_by_the_schema():
    for bad in (
        {'enough_info': True, 'overall_note': 'x', 'requirements': [{'id': 'r1', 'requirement': 'Python',
                                                                     'category': 'critical', 'status': 'demonstrated',
                                                                     'explanation': 'x'}]},
        {'enough_info': True, 'overall_note': 'x', 'requirements': [{'id': 'r1', 'requirement': 'Python',
                                                                     'category': 'required', 'status': 'uncertain',
                                                                     'explanation': 'x'}]},
    ):
        with pytest.raises(Exception):
            ExperimentAnalysis.model_validate(bad)


# --- scoring rules ----------------------------------------------------------------------------------------

def counts_analysis(**categories):
    """categories: required='DPN...' with D=demonstrated, P=partial, N=not_shown. Evidence is auto-valid."""
    reqs, i = [], 0
    for category, letters in categories.items():
        for ch in letters:
            i += 1
            status = {'D': 'demonstrated', 'P': 'partial', 'N': 'not_shown'}[ch]
            evidence = [Citation(path='skills', quote='Python')] if status != 'not_shown' else []
            reqs.append(R(f'q{i}', f'Req {i}', category, status, evidence))
    return ExperimentAnalysis(enough_info=True, overall_note='x', requirements=reqs)


@pytest.mark.parametrize('spec, expected', [
    (dict(required='DDD', standard='DD', preferred='D'), 100),
    (dict(required='NN', standard='NN', preferred='N'), 0),
    (dict(required='DDDN'), 75),                       # lone category gets the whole weight
    (dict(required='DD', preferred='N'), 80),          # 60/15 redistribute over 75
    (dict(required='DPN', standard='D'), 65),          # 1100/17 rounded
    (dict(required='D', standard='DN', preferred='N'), 73),  # 72.5 rounds half UP
    (dict(required='N', standard='DN', preferred='N'), 13),  # 12.5 rounds half UP
    (dict(required='DN'), 50),                         # exactly the two-qualification minimum
])
def test_scoring_vectors(spec, expected):
    assert evaluate(counts_analysis(**spec), CV)['score'] == expected


def test_preferred_only_posts_score_with_a_note():
    result = evaluate(counts_analysis(preferred='DP'), CV)
    assert result['score'] == 75 and result['scoreNote'] == 'preferred_only'
    assert evaluate(counts_analysis(required='DD'), CV)['scoreNote'] is None


def test_fewer_than_two_qualifications_or_insufficient_info_gives_no_score():
    one = evaluate(counts_analysis(required='D'), CV)
    assert one['score'] is None and one['scoreUnavailableReason'] == 'too_few_qualifications'
    assert one['verdict'] == 'Not enough information'
    flagged = counts_analysis(required='DDD')
    flagged.enough_info = False
    assert evaluate(flagged, CV)['scoreUnavailableReason'] == 'insufficient_information'


def test_unverifiable_citations_can_push_a_report_under_the_minimum_without_manufacturing_a_score():
    analysis = ExperimentAnalysis(enough_info=True, overall_note='x', requirements=[
        R('q1', 'Python', 'required', 'demonstrated', [Citation(path='skills', quote='Python')]),
    ])
    assert evaluate(analysis, CV)['score'] is None  # one qualification is never enough


def test_mandatory_credential_warnings_are_separate_and_never_change_the_score():
    def flag(a):
        a.requirements[4] = R('r5', 'Evaluating LLM outputs or RLHF workflows', 'required', 'not_shown',
                              mandatory_credential=True)
    flagged, plain = evaluate(mutate(flag), CV), evaluate(good_model_response(), CV)
    assert flagged['score'] == plain['score'] == 66
    (warning,) = flagged['eligibilityWarnings']
    assert warning['message'] == 'Not evidenced in your CV. Please verify.'
    assert plain['eligibilityWarnings'] == []


@pytest.mark.parametrize('score, label', [(100, 'Strong fit'), (80, 'Strong fit'), (79, 'Good fit'), (60, 'Good fit'),
                                          (59, 'Partial fit'), (40, 'Partial fit'), (39, 'Low fit'), (None, 'Not enough information')])
def test_fit_labels(score, label):
    assert verdict_for(score) == label


def test_constants_are_the_approved_ones():
    assert SHARES == {'required': 60, 'standard': 25, 'preferred': 15} and MIN_QUALIFICATIONS == 2


# --- prompt, rendering, purity ------------------------------------------------------------------------------

def test_rendered_cv_tags_every_item_and_carries_no_pii():
    text = render_cv(CV)
    for tag in ('[summary]', '[experience.exp-1.bullets.0]', '[projects.proj-1.description]',
                '[education.edu-1.degree]', '[skills] Python, SQL, Excel'):
        assert tag in text
    for pii in ('Sam Example', 'sam@example.test', '555 0100', 'Springfield', 'example.test/sam'):
        assert pii not in text


def test_prompt_carries_the_finalized_rules_and_build_input_delimits_data():
    for phrase in ('NEVER working conditions', 'personality traits', 'never extra requirements',
                   'do not turn ordinary duties into prerequisites', 'even inside a Requirements section',
                   '"X or Y" alternatives as ONE requirement', 'never guess, never invent',
                   'a mobile app project is not LLM evaluation experience', 'Do NOT output a numeric score',
                   'untrusted DATA', 'Not shown in your CV.'):
        assert phrase in SYSTEM_PROMPT, phrase
    built = build_input('QA Specialist', 'Lumen', JOB, CV)
    assert '<job_description>' in built and '[skills] Python, SQL, Excel' in built


def test_evaluate_is_pure_and_deterministic():
    analysis, cv = good_model_response(), copy.deepcopy(CV)
    first, second = evaluate(analysis, cv), evaluate(analysis, cv)
    assert first == second and cv == CV and analysis == good_model_response()


# --- documented, intentional limits of the AI-first design ---------------------------------------------------

def test_limit_the_backend_cannot_correct_a_wrong_category():
    """If the model calls remote-work comfort a Required qualification, nothing in code rejects it: the
    exclusion rules live only in the prompt. The Phase 2 verifier would have rejected this."""
    analysis = mutate(lambda a: a.requirements.append(
        R('bad', 'Comfortable working remotely', 'required', 'not_shown')))
    result = evaluate(analysis, CV)
    assert any(r['id'] == 'bad' for r in result['requirements'])
    assert result['score'] == 57 != 66  # the misclassification moves the score (required avg drops to 7/12)


def test_limit_an_authentic_but_irrelevant_quote_is_accepted():
    """The mobile-app quote is verbatim and correctly cited, so this backend keeps it: relevance is judged
    only by the model. The Phase 2/3 verifier downgrades this to not_shown."""
    analysis = mutate(lambda a: a.requirements.__setitem__(4, R(
        'r5', 'Evaluating LLM outputs or RLHF workflows', 'required', 'demonstrated',
        [Citation(path='projects.proj-2.description', quote='Built a cross-platform mobile app')],
        'Your project work shows model evaluation.')))
    result = evaluate(analysis, CV)
    assert next(r for r in result['requirements'] if r['id'] == 'r5')['status'] == 'demonstrated'
    assert result['score'] == 82 != 66  # the irrelevant evidence inflates the score (required avg rises to 9/10)
