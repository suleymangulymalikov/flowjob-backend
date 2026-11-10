"""OpenAI job-matching service on the AI-first contract (app/experiment/matching.py).

The model interprets the job post and CV under the rules in its prompt; the backend verifies job quotes
and CV citations and computes the score deterministically. All OpenAI-specific logic lives here so the
provider or model can be swapped without touching the router.

Safety/cost notes:
- The caller passes the CV content; only its PII-free, path-tagged rendering reaches the model.
- Job and CV text are delimited DATA; the prompt tells the model to ignore instructions inside them.
- No web-search or other billable tools are enabled.
- Tests monkeypatch `analyze`, so no SDK/key/network is needed and no charges occur.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.experiment.matching import (
    EXPERIMENT_PROMPT_VERSION, NOT_SHOWN_TEXT, SYSTEM_PROMPT, Citation, ExcludedItem, ExperimentAnalysis,
    Requirement, build_input,
)
from app.services.ai_provider import Usage, request_analysis

PROMPT_VERSION = f'ai-first-{EXPERIMENT_PROMPT_VERSION}'


@dataclass
class AnalysisResult:
    analysis: ExperimentAnalysis
    usage: Usage
    model: str
    prompt_version: str = PROMPT_VERSION


def _mock_result() -> AnalysisResult:
    """Deterministic canned analysis for OPENAI_API_KEY=mock (no network, no charge).

    Its citations are checked against the real CV like any model output, so with a CV that does not list
    Python/MongoDB the claims degrade honestly to not_shown instead of inventing evidence.
    """
    analysis = ExperimentAnalysis(
        enough_info=True,
        overall_note='Your CV clearly shows Python and MongoDB work; Docker is the biggest gap.',
        requirements=[
            Requirement(id='r1', requirement='Python', category='required', source_quote='Python',
                        status='demonstrated', evidence=[Citation(path='skills', quote='Python')],
                        explanation='Your Skills section lists Python.'),
            Requirement(id='r2', requirement='MongoDB', category='required', source_quote='MongoDB',
                        status='demonstrated', evidence=[Citation(path='skills', quote='MongoDB')],
                        explanation='Your Skills section lists MongoDB.'),
            Requirement(id='r3', requirement='Docker', category='preferred', source_quote='Docker',
                        status='not_shown', explanation=NOT_SHOWN_TEXT),
        ],
        excluded=[ExcludedItem(text='Build and maintain services', reason='job_duty')],
    )
    return AnalysisResult(analysis=analysis, usage=Usage(input_tokens=520, output_tokens=240), model='mock')


def analyze(job_title: str, company: str | None, job_description: str, cv_content: dict[str, Any]) -> AnalysisResult:
    """Call OpenAI once and return the validated structured analysis.

    Raises an AiError subclass on failure; the router maps these to HTTP status codes.
    With OPENAI_API_KEY=mock, returns a canned analysis without any network call or charge.
    """
    if settings.openai_api_key == 'mock':
        return _mock_result()

    user_input = build_input(job_title, company, job_description, cv_content)
    parsed, usage = request_analysis(SYSTEM_PROMPT, user_input, ExperimentAnalysis)
    return AnalysisResult(analysis=parsed, usage=usage, model=settings.openai_model)
