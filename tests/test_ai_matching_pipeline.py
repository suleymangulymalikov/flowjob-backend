"""AI-first Job Matching pipeline: deterministic tests with synthetic fixtures and mocked AI responses.

No network, no paid calls, no database writes: `evaluate` is pure. The "AI responses" below are hand-written
`MatchAnalysis` objects — both a well-behaved model following the finalized rules on a RemoExperts-style
post, and misbehaving ones, to show exactly what this minimal backend does and does not catch.
"""
import copy
from fractions import Fraction

import pytest

from app.ai_matching.matching import (
    MIN_QUALIFICATIONS, NOT_SHOWN_TEXT, SHARES, SYSTEM_PROMPT, Citation, ExcludedItem, MatchAnalysis,
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


def R(id, text, category, status, evidence=(), explanation=None, source_quote=None, **kw):
    return Requirement(id=id, requirement=text, category=category, status=status, evidence=list(evidence),
                       source_quote=source_quote if source_quote is not None else text,
                       explanation=explanation or (NOT_SHOWN_TEXT if status == 'not_shown' else f'Your CV shows {text}.'),
                       **kw)


def good_model_response():
    """What a model following the finalized rules returns for JOB + CV."""
    return MatchAnalysis(
        enough_info=True,
        overall_note='Your CV clearly shows annotation QA and documentation work; LLM evaluation is the biggest gap.',
        requirements=[
            R('r1', 'QA on annotated data', 'required', 'demonstrated', source_quote='Experience performing quality assurance on annotated or labelled data',
              evidence=[Citation(path='experience.exp-1.bullets.0', quote='Reviewed 300+ annotated text samples per week')],
              explanation='Your internship reviewed hundreds of annotated samples weekly against guidelines.'),
            R('r2', 'Written documentation skills', 'required', 'demonstrated',
              [Citation(path='experience.exp-1.bullets.1', quote='Wrote and updated a shared documentation page')],
              source_quote='Strong written documentation skills'),
            R('r3', 'Data pipelines with Python and SQL', 'required', 'partial',
              [Citation(path='skills', quote='Python, SQL')],
              'You list Python and SQL, but no pipeline work is shown — a meaningful component only.',
              source_quote='Experience building data pipelines with Python and SQL'),
            R('r4', 'Python or R for data analysis', 'required', 'demonstrated',
              [Citation(path='skills', quote='Python')],
              source_quote='Proficiency in Python or R for data analysis'),
            R('r5', 'Evaluating LLM outputs or RLHF workflows', 'required', 'not_shown',
              source_quote='Hands-on experience evaluating LLM outputs or RLHF workflows'),
            R('r6', 'Annotation tools such as Label Studio', 'preferred', 'not_shown',
              source_quote='annotation tools such as Label Studio (preferred)'),
            R('r7', 'Dashboards in Power BI', 'preferred', 'demonstrated',
              [Citation(path='projects.proj-1.description', quote='Built a Power BI dashboard')],
              source_quote='Experience building dashboards in Power BI'),
        ],
        excluded=[
            ExcludedItem(text='Comfortable working remotely with a distributed team', reason='working_condition'),
            ExcludedItem(text='Passionate about data quality', reason='personality_trait'),
            ExcludedItem(text='Build simple dashboards to track annotation quality', reason='job_duty'),
        ],
    )


# --- the happy path: RemoExperts-style scenario -----------------------------------------------------------

def test_remoexperts_scenario_scores_the_approved_way():
    result = evaluate(good_model_response(), CV, JOB)
    # Required: 3 demonstrated + 1 partial + 1 not_shown of 5 -> 7/10. Preferred: 1 of 2 -> 1/2. Standard empty.
    # 100 * (60*(7/10) + 15*(1/2)) / 75 = 66
    assert result['score'] == 66 and result['verdict'] == 'Good fit'
    assert result['qualificationCounts']['required'] == {'demonstrated': 3, 'partial': 1, 'not_shown': 1, 'count': 5}
    assert result['qualificationCounts']['standard']['count'] == 0
    assert result['diagnostics'] == {'citations_dropped': 0, 'statuses_downgraded': 0, 'duplicate_ids': 0,
                                     'rejected_no_job_quote': 0, 'rejected_working_condition': 0,
                                     'citations_path_corrected': 0, 'droppedCitations': []}


def test_working_conditions_duties_and_traits_are_outside_the_scored_set():
    result = evaluate(good_model_response(), CV, JOB)
    scored = {r['requirement'] for r in result['requirements']}
    assert not scored & {'Comfortable working remotely with a distributed team', 'Passionate about data quality',
                         'Build simple dashboards to track annotation quality'}
    assert {e['reason'] for e in result['excluded']} == {'working_condition', 'personality_trait', 'job_duty'}


def test_llm_evaluation_is_not_shown_with_the_neutral_sentence_and_no_evidence():
    llm = next(r for r in evaluate(good_model_response(), CV, JOB)['requirements'] if r['id'] == 'r5')
    assert llm['status'] == 'not_shown' and llm['evidence'] == [] and llm['explanation'] == NOT_SHOWN_TEXT


def test_combined_and_alternative_requirements_are_single_entries():
    result = evaluate(good_model_response(), CV, JOB)
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
        'Your internship evaluated LLM outputs.',
        source_quote='Hands-on experience evaluating LLM outputs or RLHF workflows')))
    result = evaluate(analysis, CV, JOB)
    llm = next(r for r in result['requirements'] if r['id'] == 'r5')
    assert llm['status'] == 'not_shown' and llm['explanation'] == NOT_SHOWN_TEXT
    assert result['score'] == 66  # identical to the honest answer
    diag = result['diagnostics']
    assert (diag['citations_dropped'], diag['statuses_downgraded']) == (1, 1)
    assert diag['droppedCitations'] == [{'id': 'r5', 'path': 'experience.exp-1.bullets.0',
                                         'quote': 'evaluated LLM outputs for RLHF'}]  # drops are never silent


@pytest.mark.parametrize('path, quote', [
    ('experience.exp-9.bullets.0', 'Reviewed 300+ annotated text samples'),   # no such role
    ('projects.proj-1.description', 'annotated text samples'),                # in no field of that entry
    ('projects.proj-1.tech', 'Cleaned a CSV text dataset with Python'),       # text of ANOTHER project
    ('experience.exp-1.bullets.0', 'Built a Power BI dashboard'),             # text of a project, cited as a role
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
        [Citation(path='projects.proj-1.tech', quote='Power BI')],
        source_quote='Experience building dashboards in Power BI')))
    result = evaluate(analysis, CV, JOB)
    assert result['qualificationCounts']['preferred']['count'] == 2 and result['diagnostics']['duplicate_ids'] == 1


def test_malformed_output_is_rejected_by_the_schema():
    for bad in (
        {'enough_info': True, 'overall_note': 'x', 'requirements': [{'id': 'r1', 'requirement': 'Python',
                                                                     'category': 'critical', 'status': 'demonstrated',
                                                                     'source_quote': 'Python', 'explanation': 'x'}]},
        {'enough_info': True, 'overall_note': 'x', 'requirements': [{'id': 'r1', 'requirement': 'Python',
                                                                     'category': 'required', 'status': 'uncertain',
                                                                     'source_quote': 'Python', 'explanation': 'x'}]},
        {'enough_info': True, 'overall_note': 'x', 'requirements': [{'id': 'r1', 'requirement': 'Python',
                                                                     'category': 'required', 'status': 'demonstrated',
                                                                     'explanation': 'x'}]},  # no source_quote
    ):
        with pytest.raises(Exception):
            MatchAnalysis.model_validate(bad)


# --- scoring rules ----------------------------------------------------------------------------------------

COUNTS_JOB = 'Requirements\n' + '\n'.join(f'- Req {i}' for i in range(1, 15))


def counts_analysis(**categories):
    """categories: required='DPN...' with D=demonstrated, P=partial, N=not_shown. Evidence is auto-valid."""
    reqs, i = [], 0
    for category, letters in categories.items():
        for ch in letters:
            i += 1
            status = {'D': 'demonstrated', 'P': 'partial', 'N': 'not_shown'}[ch]
            evidence = [Citation(path='skills', quote='Python')] if status != 'not_shown' else []
            reqs.append(R(f'q{i}', f'Req {i}', category, status, evidence))
    return MatchAnalysis(enough_info=True, overall_note='x', requirements=reqs)


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
    assert evaluate(counts_analysis(**spec), CV, COUNTS_JOB)['score'] == expected


def test_preferred_only_posts_score_with_a_note():
    result = evaluate(counts_analysis(preferred='DP'), CV, COUNTS_JOB)
    assert result['score'] == 75 and result['scoreNote'] == 'preferred_only'
    assert evaluate(counts_analysis(required='DD'), CV, COUNTS_JOB)['scoreNote'] is None


def test_fewer_than_two_qualifications_or_insufficient_info_gives_no_score():
    one = evaluate(counts_analysis(required='D'), CV, COUNTS_JOB)
    assert one['score'] is None and one['scoreUnavailableReason'] == 'too_few_qualifications'
    assert one['verdict'] == 'Not enough information'
    flagged = counts_analysis(required='DDD')
    flagged.enough_info = False
    assert evaluate(flagged, CV, COUNTS_JOB)['scoreUnavailableReason'] == 'insufficient_information'


def test_unverifiable_citations_can_push_a_report_under_the_minimum_without_manufacturing_a_score():
    analysis = MatchAnalysis(enough_info=True, overall_note='x', requirements=[
        R('q1', 'Python', 'required', 'demonstrated', [Citation(path='skills', quote='Python')],
          source_quote='Proficiency in Python'),
    ])
    assert evaluate(analysis, CV, JOB)['score'] is None  # one qualification is never enough


def test_mandatory_credential_warnings_are_separate_and_never_change_the_score():
    def flag(a):
        a.requirements[4] = R('r5', 'Evaluating LLM outputs or RLHF workflows', 'required', 'not_shown',
                              source_quote='Hands-on experience evaluating LLM outputs or RLHF workflows',
                              mandatory_credential=True)
    flagged, plain = evaluate(mutate(flag), CV, JOB), evaluate(good_model_response(), CV, JOB)
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
                   'A mobile app project is not LLM evaluation experience',
                   'NOT evidence just because it is authentic', 'never transfers to a claim about another',
                   'never stretch a loosely related quote to fit', 'rejects any requirement whose quote it cannot find',
                   'Do NOT output a numeric score',
                   'untrusted DATA', 'Not shown in your CV.'):
        assert phrase in SYSTEM_PROMPT, phrase
    built = build_input('QA Specialist', 'Lumen', JOB, CV)
    assert '<job_description>' in built and '[skills] Python, SQL, Excel' in built


def test_evaluate_is_pure_and_deterministic():
    analysis, cv = good_model_response(), copy.deepcopy(CV)
    first, second = evaluate(analysis, cv, JOB), evaluate(analysis, cv, JOB)
    assert first == second and cv == CV and analysis == good_model_response()


# --- documented, intentional limits of the AI-first design ---------------------------------------------------

def test_backstop_a_working_condition_scored_by_the_model_is_excluded_not_scored():
    """Even if the model classifies remote-work comfort as Required, the backstop keeps it out of the score."""
    analysis = mutate(lambda a: a.requirements.append(
        R('bad', 'Comfortable working remotely', 'required', 'not_shown',
          source_quote='Comfortable working remotely with a distributed team')))
    result = evaluate(analysis, CV, JOB)
    assert all(r['id'] != 'bad' for r in result['requirements'])
    assert result['rejected'] == [{'id': 'bad', 'requirement': 'Comfortable working remotely', 'reason': 'working_condition',
                                   'quote': 'Comfortable working remotely with a distributed team'}]
    assert result['score'] == 66  # unchanged
    assert result['diagnostics']['rejected_working_condition'] == 1


@pytest.mark.parametrize('text, quote', [
    ('Full-time availability', 'This is a remote, full-time contract role'),
    ('Right to work in the UK', 'This is a remote, full-time contract role'),
])
def test_backstop_catches_other_arrangement_and_authorization_wordings(text, quote):
    analysis = mutate(lambda a: a.requirements.append(R('bad', text, 'required', 'not_shown', source_quote=quote)))
    result = evaluate(analysis, CV, JOB)
    assert all(r['id'] != 'bad' for r in result['requirements']) and result['score'] == 66


def test_backstop_leaves_professional_skills_alone_even_when_they_sound_similar():
    job = ('Requirements\n- Experience with remote sensing data.\n- Shift-left testing experience.\n'
           '- Contract law knowledge.\n- Experience managing remote teams.\n')
    reqs = [R(f'q{i}', t, 'required', 'not_shown', source_quote=t)
            for i, t in enumerate(('Experience with remote sensing data', 'Shift-left testing experience',
                                   'Contract law knowledge', 'Experience managing remote teams'))]
    result = evaluate(MatchAnalysis(enough_info=True, overall_note='x', requirements=reqs), CV, job)
    assert len(result['requirements']) == 4 and result['rejected'] == []


def test_an_invented_requirement_is_rejected_because_its_quote_is_not_in_the_post():
    analysis = mutate(lambda a: a.requirements.append(
        R('inv', 'Kubernetes', 'required', 'not_shown', source_quote='Experience with Kubernetes clusters')))
    result = evaluate(analysis, CV, JOB)
    assert all(r['id'] != 'inv' for r in result['requirements'])
    assert result['rejected'] == [{'id': 'inv', 'requirement': 'Kubernetes', 'reason': 'quote_not_in_job',
                                   'quote': 'Experience with Kubernetes clusters'}]
    assert result['score'] == 66 and result['diagnostics']['rejected_no_job_quote'] == 1


def test_job_quotes_fold_case_and_whitespace_but_not_paraphrase():
    ok = mutate(lambda a: a.requirements.__setitem__(1, R(
        'r2', 'Written documentation skills', 'required', 'demonstrated',
        [Citation(path='experience.exp-1.bullets.1', quote='Wrote and updated a shared documentation page')],
        source_quote='STRONG   written documentation skills')))
    assert evaluate(ok, CV, JOB)['score'] == 66
    paraphrased = mutate(lambda a: a.requirements.__setitem__(1, R(
        'r2', 'Written documentation skills', 'required', 'demonstrated',
        [Citation(path='experience.exp-1.bullets.1', quote='Wrote and updated a shared documentation page')],
        source_quote='Good documentation writing skills')))
    result = evaluate(paraphrased, CV, JOB)
    assert result['diagnostics']['rejected_no_job_quote'] == 1 and result['score'] != 66


def test_rejections_can_push_a_report_under_the_minimum_without_manufacturing_a_score():
    reqs = [R('q1', 'Python', 'required', 'demonstrated', [Citation(path='skills', quote='Python')],
              source_quote='Proficiency in Python or R for data analysis'),
            R('q2', 'Kubernetes', 'required', 'not_shown', source_quote='Not a line from this post')]
    result = evaluate(MatchAnalysis(enough_info=True, overall_note='x', requirements=reqs), CV, JOB)
    assert result['score'] is None and result['scoreUnavailableReason'] == 'too_few_qualifications'


def test_limit_an_authentic_but_irrelevant_quote_is_accepted():
    """The mobile-app quote is verbatim and correctly cited, so this backend keeps it: relevance is enforced
    only through the model's instructions. The Phase 2/3 verifier downgrades this to not_shown."""
    analysis = mutate(lambda a: a.requirements.__setitem__(4, R(
        'r5', 'Evaluating LLM outputs or RLHF workflows', 'required', 'demonstrated',
        [Citation(path='projects.proj-2.description', quote='Built a cross-platform mobile app')],
        'Your project work shows model evaluation.',
        source_quote='Hands-on experience evaluating LLM outputs or RLHF workflows')))
    result = evaluate(analysis, CV, JOB)
    assert next(r for r in result['requirements'] if r['id'] == 'r5')['status'] == 'demonstrated'
    assert result['score'] == 82 != 66  # the irrelevant evidence inflates the score (required avg rises to 9/10)


# --- wrong-field citations (same entry only) ---------------------------------------------------------------

@pytest.mark.parametrize('path, quote, resolved', [
    ('projects.proj-1.tech', 'Built a Power BI dashboard', 'projects.proj-1.description'),   # the live-test mix-up
    ('projects.proj-1.name', 'on a SQL sales database', 'projects.proj-1.description'),
    ('experience.exp-1.bullets.1', 'Reviewed 300+ annotated text samples', 'experience.exp-1.bullets.0'),  # same role
    ('experience.exp-1.title', 'Wrote and updated a shared documentation page', 'experience.exp-1.bullets.1'),
    ('[projects.proj-1.tech]', 'Built a Power BI dashboard', 'projects.proj-1.description'),
])
def test_a_quote_cited_under_the_wrong_field_moves_to_the_one_matching_field_of_the_same_entry(path, quote, resolved):
    from app.ai_matching.matching import resolve_citation
    assert resolve_citation(CV, Citation(path=path, quote=quote)) == resolved


def test_a_correctly_cited_path_is_never_rewritten():
    from app.ai_matching.matching import resolve_citation
    assert resolve_citation(CV, Citation(path='projects.proj-1.tech', quote='Power BI')) == 'projects.proj-1.tech'


def test_an_ambiguous_wrong_field_quote_is_rejected_not_guessed():
    from app.ai_matching.matching import resolve_citation
    # "Power BI" appears in BOTH the tech and description of proj-1; cited under .name it matches two fields.
    assert resolve_citation(CV, Citation(path='projects.proj-1.name', quote='Power BI')) is None


@pytest.mark.parametrize('path', ['summary', 'skills', 'languages', 'links.0'])
def test_top_level_items_have_no_sibling_fields_to_resolve_to(path):
    from app.ai_matching.matching import resolve_citation
    assert resolve_citation(CV, Citation(path=path, quote='Built a Power BI dashboard')) is None


def test_a_corrected_citation_is_scored_stored_under_its_real_path_and_counted():
    analysis = mutate(lambda a: a.requirements.__setitem__(6, R(
        'r7', 'Dashboards in Power BI', 'preferred', 'demonstrated',
        [Citation(path='projects.proj-1.tech', quote='Built a Power BI dashboard')],
        source_quote='Experience building dashboards in Power BI')))
    result = evaluate(analysis, CV, JOB)
    dash = next(r for r in result['requirements'] if r['id'] == 'r7')
    assert dash['status'] == 'demonstrated' and dash['evidence'][0]['path'] == 'projects.proj-1.description'
    assert result['diagnostics']['citations_path_corrected'] == 1 and result['diagnostics']['citations_dropped'] == 0
    assert result['score'] == 66


# --- the summary is built from verified statuses, in the second person ------------------------------------

def test_summary_is_second_person_and_agrees_with_the_verified_statuses():
    result = evaluate(good_model_response(), CV, JOB)
    assert result['overallAssessment'] == (
        'Your CV clearly shows QA on annotated data and Written documentation skills. '
        'Data pipelines with Python and SQL is only partly shown, so it is the biggest gap to close.')
    text = result['overallAssessment']
    assert text.startswith('Your CV clearly shows') and 'candidate' not in text.lower() and 'the CV' not in text


def test_summary_follows_a_downgrade_rather_than_the_models_claim():
    lying = mutate(lambda a: a.requirements.__setitem__(0, R(
        'r1', 'QA on annotated data', 'required', 'demonstrated',
        [Citation(path='experience.exp-1.bullets.0', quote='invented quote')],
        source_quote='Experience performing quality assurance on annotated or labelled data')))
    text = evaluate(lying, CV, JOB)['overallAssessment']
    assert 'QA on annotated data' not in text.split('.')[0]          # no longer listed as a strength
    assert 'biggest gap to address is QA on annotated data' in text   # reported as the (required) gap


def test_summary_when_everything_is_shown_or_nothing_is_scorable():
    full = evaluate(counts_analysis(required='DD'), CV, COUNTS_JOB)['overallAssessment']
    assert full.endswith('Every qualification this post lists is shown in your CV.')
    none = evaluate(counts_analysis(required='D'), CV, COUNTS_JOB)['overallAssessment']
    assert 'aren’t enough clearly stated qualifications' in none


# --- job quotes: only harmless formatting differences are tolerated ---------------------------------------

POWER_BI_LINE = 'Experience building dashboards in Power BI'


@pytest.mark.parametrize('quote', [
    'Experience building dashboards in Power BI.',            # as in the post
    f'- {POWER_BI_LINE}.',                                     # leading bullet marker
    f'• {POWER_BI_LINE}',
    f'1. {POWER_BI_LINE}',
    f'Nice to Have: {POWER_BI_LINE}',                          # heading before the qualification
    f'Nice to Have\n- {POWER_BI_LINE}.',                       # heading on its own line + marker
    f'NICE TO HAVE\n{POWER_BI_LINE}',
])
def test_harmless_formatting_differences_in_a_job_quote_are_accepted(quote):
    from app.ai_matching.matching import quote_in_job
    assert quote_in_job(JOB, quote)


@pytest.mark.parametrize('quote', [
    'Experience with dashboards in Power BI',                  # paraphrase
    f'Optional Extras: {POWER_BI_LINE}',                       # a "heading" that is not in the post
    f'Nice to Have\n- {POWER_BI_LINE}\n- Familiarity with annotation tools such as Label Studio',  # two qualifications
    'Nice to Have: Experience building dashboards in Tableau', # invented remainder
    'Nice to Have\n- Experience building dashboards in Power BI\n- Familiarity with annotation tools such as Label Studio',  # two lines, the first is a qualification
    'Requirements\nNice to Have\n- Experience building dashboards in Power BI',  # more than one heading line
    '- Nice to Have\n- Experience building dashboards in Power BI',  # a marked line is not a heading
    '- ',
    '',
])
def test_paraphrases_invented_text_and_multiple_lines_are_still_rejected(quote):
    from app.ai_matching.matching import quote_in_job
    assert not quote_in_job(JOB, quote)


def test_a_heading_prefixed_quote_is_scored_and_a_rejection_explains_itself():
    ok = mutate(lambda a: a.requirements.__setitem__(6, R(
        'r7', 'Dashboards in Power BI', 'preferred', 'demonstrated',
        [Citation(path='projects.proj-1.description', quote='Built a Power BI dashboard')],
        source_quote=f'Nice to Have: {POWER_BI_LINE}')))
    result = evaluate(ok, CV, JOB)
    assert result['score'] == 66 and result['rejected'] == []
    bad = mutate(lambda a: a.requirements.__setitem__(6, R(
        'r7', 'Dashboards in Power BI', 'preferred', 'demonstrated',
        [Citation(path='projects.proj-1.description', quote='Built a Power BI dashboard')],
        source_quote='Experience with dashboards in Power BI')))
    out = evaluate(bad, CV, JOB)
    (entry,) = out['rejected']
    assert entry['reason'] == 'quote_not_in_job' and entry['quote'] == 'Experience with dashboards in Power BI'
    assert JOB not in str(out['rejected'])  # only the short claimed quote, never the post
