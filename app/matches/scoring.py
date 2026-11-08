"""Deterministic, documented match-score rubric.

The model does NOT return the score. It returns requirements, each with an importance
(required/preferred) and an evidence status grounded in the CV. The backend computes the
score here so it is transparent, reproducible and explainable.

Score = 100 * (earned weight) / (total possible weight), where:
  weight(required) = 2.0, weight(preferred) = 1.0
  credit(clearly_demonstrated) = 1.0
  credit(partially_demonstrated) = 0.5
  credit(uncertain) = 0.25
  credit(not_mentioned) = 0.0

The score is returned as None ("unavailable") when the model reports it lacked enough
information, or when too few requirements were identified to be meaningful.
"""
from app.matches.schemas import AiAnalysis, EvidenceStatus, Importance, RequirementAssessment

WEIGHT = {Importance.required: 2.0, Importance.preferred: 1.0}
CREDIT = {
    EvidenceStatus.clearly_demonstrated: 1.0,
    EvidenceStatus.partially_demonstrated: 0.5,
    EvidenceStatus.uncertain: 0.25,
    EvidenceStatus.not_mentioned: 0.0,
}
MIN_REQUIREMENTS_FOR_SCORE = 3


def compute_score(analysis: AiAnalysis) -> int | None:
    reqs = analysis.requirements
    if not analysis.enough_info or len(reqs) < MIN_REQUIREMENTS_FOR_SCORE:
        return None
    total = sum(WEIGHT[r.importance] for r in reqs)
    if total == 0:
        return None
    earned = sum(WEIGHT[r.importance] * CREDIT[r.status] for r in reqs)
    return round(100 * earned / total)


def verdict_for(score: int | None) -> str:
    if score is None:
        return 'Not enough information'
    if score >= 80:
        return 'Strong fit'
    if score >= 60:
        return 'Good fit'
    if score >= 40:
        return 'Partial fit'
    return 'Low fit'


def _count(reqs: list[RequirementAssessment], status: EvidenceStatus) -> int:
    return sum(1 for r in reqs if r.status == status)


def explain(analysis: AiAnalysis, score: int | None) -> str:
    """A plain-language, deterministic explanation of how the score was derived."""
    reqs = analysis.requirements
    if score is None:
        return (
            'Not enough identifiable requirements in this job post to score reliably. '
            'Paste the full description, including the requirements section.'
        )
    required = sum(1 for r in reqs if r.importance == Importance.required)
    preferred = len(reqs) - required
    clear = _count(reqs, EvidenceStatus.clearly_demonstrated)
    partial = _count(reqs, EvidenceStatus.partially_demonstrated)
    missing = _count(reqs, EvidenceStatus.not_mentioned)
    uncertain = _count(reqs, EvidenceStatus.uncertain)
    return (
        f'Scored {score}/100 across {len(reqs)} requirements '
        f'({required} required, {preferred} preferred). '
        f'{clear} clearly demonstrated, {partial} partially, {uncertain} uncertain, '
        f'{missing} not mentioned. Required requirements count double, and each requirement '
        f'earns full, half, quarter or no credit by how clearly your CV supports it.'
    )
