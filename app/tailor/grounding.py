"""Bind provider proposals to specific text in an immutable source revision."""

import hashlib
import json
from typing import Any

from app.tailor.schemas import ChangeSection, ProposedChange


def content_revision(content: dict[str, Any]) -> str:
    # Object key order isn't meaningful. Array order (in particular, bullet positions) is.
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _item(items: Any, item_id: str) -> dict | None:
    if not isinstance(items, list):
        return None
    matches = [item for item in items if isinstance(item, dict) and item.get('id') is not None
               and str(item['id']) == item_id]
    return matches[0] if len(matches) == 1 else None


def target_text(content: dict[str, Any], change: ProposedChange) -> str | None:
    """Resolve exactly one target. Missing, ambiguous or conflicting selectors are invalid."""
    if change.section == ChangeSection.experience:
        if not change.experience_id or change.bullet_index is None or change.project_id is not None:
            return None
        item = _item(content.get('experience') or [], change.experience_id)
        bullets = item.get('bullets') if item else None
        if isinstance(bullets, list) and 0 <= change.bullet_index < len(bullets):
            bullet = bullets[change.bullet_index]
            return bullet if isinstance(bullet, str) else None
        return None

    if change.experience_id is not None or change.bullet_index is not None:
        return None
    if change.section == ChangeSection.project:
        if not change.project_id:
            return None
        item = _item(content.get('projects') or [], change.project_id)
        text = item.get('description') if item else None
        return text if isinstance(text, str) else None

    if change.project_id is not None:
        return None
    if change.section == ChangeSection.summary:
        text = content.get('summary')
        return text if isinstance(text, str) else None
    skills = content.get('skills')
    if isinstance(skills, list) and all(isinstance(skill, str) for skill in skills):
        return ', '.join(skills)
    return None


def safe_to_auto_apply(change: ProposedChange, original: str, content: dict[str, Any]) -> bool:
    """Allow formatting changes we can verify without judging the truth of a paraphrase."""
    if change.section == ChangeSection.skills:
        before = sorted(skill.strip().casefold() for skill in content['skills'])
        after = sorted(skill.strip().casefold() for skill in change.suggested.split(',') if skill.strip())
        return before == after

    def wording(text: str) -> str:
        # Leave internal punctuation, numbers, words and their order intact.
        return ' '.join(text.split()).removesuffix('.').casefold()

    return wording(original) == wording(change.suggested)
