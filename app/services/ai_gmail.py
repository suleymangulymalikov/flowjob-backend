"""OpenAI recruitment-email classification on the shared provider (app/services/ai_provider.py).

Only the minimized email text, subject, sender name/domain and the user's application shortlist reach the model;
never OAuth credentials, attachments or full headers. No tools are enabled and responses are not stored
(``store=False``). Tests monkeypatch ``analyze``; OPENAI_API_KEY=mock returns an empty analysis with no network.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.gmail.classify import GMAIL_PROMPT_VERSION, SYSTEM_PROMPT, EmailAnalysis
from app.services.ai_provider import Usage, request_analysis, use_mock_ai


@dataclass
class GmailAnalysisResult:
    analysis: EmailAnalysis
    usage: Usage
    model: str
    prompt_version: str = GMAIL_PROMPT_VERSION


def analyze(user_input: str) -> GmailAnalysisResult:
    """One structured call per candidate email. Raises an AiError subclass on failure."""
    if use_mock_ai('Gmail analysis'):
        return GmailAnalysisResult(EmailAnalysis(is_recruitment=False, items=[]), Usage(0, 0), 'mock')
    parsed, usage = request_analysis(
        SYSTEM_PROMPT, user_input, EmailAnalysis,
        max_output_tokens=settings.gmail_max_output_tokens,
        timeout=settings.gmail_timeout_seconds,
        temperature=0.0,
    )
    return GmailAnalysisResult(analysis=parsed, usage=usage, model=settings.openai_model)
