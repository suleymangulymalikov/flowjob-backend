"""Resolve and validate AI-proposed CV changes against the immutable source CV.

The model addresses targets by path and proposes typed changes. This module is the only judge of what may be
shown to the user: it resolves each path, fills in the original text, requires exact permutations for reorders and a
verbatim in-CV quotation for added skills, and drops cosmetic no-ops. Only a verified reorder is ever auto-previewed;
wording is never trusted as "safe". It deliberately does NOT try to decide whether a rewrite is factual: rewrites,
deletions and new skills always need the user's approval.
"""
from __future__ import annotations

import re
from typing import Any

from app.ai_matching.matching import Citation, _fold, _items, resolve_citation
from app.tailor.schemas import ProposedChange

_BULLET_PATH = re.compile(r'^experience\.(?P<id>.+)\.bullets\.(?P<n>\d+)$')
_PROJECT_PATH = re.compile(r'^projects\.(?P<id>.+)\.description$')
_ROLE_PATH = re.compile(r'^experience\.(?P<id>[^.]+)$')
MAX_ADDED_SKILLS = 3


def _one(items: Any, item_id: str) -> dict | None:
    if not isinstance(items, list):
        return None
    hits = [i for i in items if isinstance(i, dict) and i.get('id') is not None and str(i['id']) == item_id]
    return hits[0] if len(hits) == 1 else None


def _skill_key(skill: str) -> str:
    return re.sub(r'[^0-9a-z+#]', '', _fold(skill))


_NUMBER = re.compile(r'\d+(?:[.,]\d+)*')
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen '
    'eighteen nineteen twenty'.split())} | {'thirty': '30', 'forty': '40', 'fifty': '50', 'hundred': '100', 'thousand': '1000'}
_WORD = re.compile(r'[0-9a-z+#]+')
_STOP = frozenset(
    'a an and are as at be been but by for from had has have in into is it its of on or over that the their this to '
    'under using used was were which while who will with within your you i my our we they them than then also across '
    'about more most such including via per each other both all any some new'.split())
# Self-praise and filler a CV line cannot evidence. Refused unless the CV itself already uses the wording.
# Words that say nothing checkable on their own: listing them would bury the ones the user must actually verify.
_GENERIC = frozenset(
    'experience experienced work worked working role roles skill skills skilled task tasks team teams '
    'including include includes various several part time full well also both used using use made make making '
    'background focus focused focusing area areas field level range related relevant based across within through '
    'developed develop building built build created create creating '
    'responsible involved helped help support supported supporting provided provide performed perform completed '
    'complete conducted carried contributed applied apply'.split())
_UNSUPPORTED = re.compile(
    r'\b(?:seeking (?:to|a|an)|looking (?:to|for)|aiming to|eager to|detail[- ]oriented|results?[- ](?:driven|oriented)|'
    r'highly motivated|self[- ]motivated|dedicated professional|proven track record|passionate|ensur(?:e|es|ed|ing)|'
    r'thorough(?:ly)?|successfully|effectively|efficiently|strong foundation|leverag(?:e|ing)|fast[- ]paced|'
    r'excellent|exceptional|extensive|seasoned|dynamic)\b')


def _numbers(text: str) -> set[str]:
    """Figures in the text, digits or spelled out ("seven years" counts as 7)."""
    spelled = {_NUMBER_WORDS[w] for w in _WORD.findall(_fold(text)) if w in _NUMBER_WORDS}
    return {re.sub(r'[.,]', '', n) for n in _NUMBER.findall(text)} | spelled


def _stem(word: str) -> str:
    for suffix in ('ising', 'izing', 'ation', 'ing', 'ised', 'ized', 'ed', 'es', 'ly', 's'):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(_fold(text)) if w not in _STOP and not w.isdigit()}


def new_terms(suggested: str, cv_text: str) -> list[str]:
    """Content words of a rewrite that appear nowhere in the CV, in order: shown to the user so that anything the
    model brought in from outside the CV (often the job post's wording) is visible before they approve it."""
    known, seen, out = _stems(cv_text), set(), []
    for word in _WORD.findall(_fold(suggested)):
        stem = _stem(word)
        if len(word) >= 4 and word not in _STOP and word not in _GENERIC and not word.isdigit() \
                and word not in _NUMBER_WORDS and stem not in known and stem not in seen:
            seen.add(stem)
            out.append(word)
    return out[:10]


def _repeats_another(bullets: list[str], n: int) -> bool:
    """The bullet shares most of its content words with another bullet of the same role."""
    mine = _stems(bullets[n])
    for i, other in enumerate(bullets):
        theirs = _stems(other) if i != n else set()
        shared = mine & theirs
        if len(shared) >= 2 and len(shared) / max(1, min(len(mine), len(theirs))) >= 0.6:
            return True
    return False


def _same_wording(a: str, b: str) -> bool:
    """Whitespace, case and a trailing full stop are not a meaningful change."""
    norm = lambda t: ' '.join(t.split()).removesuffix('.').casefold()  # noqa: E731
    return norm(a) == norm(b)


def _names(skill: str, quote: str) -> bool:
    """The skill is named in the quotation (whole words; spacing/punctuation inside the name is flexible)."""
    parts = [re.escape(p) for p in re.findall(r'[0-9a-z+#]+', _fold(skill))]
    return bool(parts) and bool(re.search(r'(?<![0-9a-z])' + r'[\s\-_.]*'.join(parts) + r'(?![0-9a-z])', _fold(quote)))


def _field_naming(content: dict[str, Any], path: str, skill: str) -> tuple[str, str] | None:
    """When the cited quote is real but does not itself contain the skill, use the shortest other field of the SAME
    role/project/education entry that does (e.g. the project's `tech` for a quote taken from its `name`)."""
    parts = path.split('.')
    if len(parts) < 3 or parts[0] not in ('experience', 'education', 'projects'):
        return None
    entry = '.'.join(parts[:2]) + '.'
    hits = [(p, t) for p, t in _items(content).items() if p.startswith(entry) and _names(skill, t)]
    return min(hits, key=lambda pair: len(pair[1])) if hits else None


def _section_fields(path: str, content: dict[str, Any]) -> dict[str, Any] | None:
    """Legacy-shaped location of a path (section + ids) so existing clients can address the target."""
    if path == 'summary':
        return {'section': 'summary'}
    if path == 'skills':
        return {'section': 'skills'}
    if m := _BULLET_PATH.match(path):
        return {'section': 'experience', 'experienceId': m['id'], 'bulletIndex': int(m['n'])}
    if m := _PROJECT_PATH.match(path):
        return {'section': 'project', 'projectId': m['id']}
    if m := _ROLE_PATH.match(path):
        return {'section': 'experience', 'experienceId': m['id']}
    return None


def ground(content: dict[str, Any], change: ProposedChange) -> tuple[dict[str, Any] | None, str | None]:
    """(suggestion fields, None) for a valid change, or (None, rejection reason)."""
    where = _section_fields(change.path, content)
    if where is None:
        return None, 'unknown_path'
    out: dict[str, Any] = {**where, 'op': change.op, 'explanation': change.reason.strip(), 'risk': 'review'}
    if change.job_requirement:
        out['jobRequirement'] = change.job_requirement

    skills = content.get('skills')
    skills = skills if isinstance(skills, list) and all(isinstance(x, str) for x in skills) else []

    def bullet_text() -> tuple[str | None, str | None]:
        m = _BULLET_PATH.match(change.path)
        role = _one(content.get('experience'), m['id']) if m else None
        bullets = role.get('bullets') if role else None
        n = int(m['n']) if m else -1
        return (bullets[n], None) if isinstance(bullets, list) and 0 <= n < len(bullets) and isinstance(bullets[n], str) \
            else (None, 'unknown_target')

    if change.op == 'rewrite':
        if where['section'] == 'summary':
            original = content.get('summary') if isinstance(content.get('summary'), str) else None
        elif where['section'] == 'project':
            project = _one(content.get('projects'), where['projectId'])
            original = project.get('description') if project and isinstance(project.get('description'), str) else None
        elif where['section'] == 'experience' and 'bulletIndex' in where:
            original, _ = bullet_text()
        else:
            return None, 'rewrite_target_not_text'
        text = (change.suggested or '').strip()
        # A CV with no summary may be offered one (built from the CV's own facts); every other target must exist.
        writing_summary = where['section'] == 'summary' and not (content.get('summary') or '').strip() \
            and content.get('summary') in (None, '')
        if writing_summary:
            original = ''
        elif not original or not original.strip():
            return None, 'unknown_target'
        if not text:
            return None, 'empty_suggestion'
        if not writing_summary and _same_wording(original, text):
            return None, 'cosmetic_no_change'
        cv_text = ' '.join(_items(content).values())
        # A figure that appears nowhere in the CV is an invented fact, whatever the wording around it.
        if _numbers(text) - _numbers(cv_text):
            return None, 'adds_number'
        folded_cv = _fold(cv_text)
        if any(m.group(0) not in folded_cv for m in _UNSUPPORTED.finditer(_fold(text))):
            return None, 'adds_unsupported_claim'
        added = new_terms(text, cv_text)
        return {**out, 'original': original, 'suggested': text, **({'newTerms': added} if added else {})}, None

    if change.op == 'delete':
        if where['section'] == 'skills':
            target = next((s for s in skills if _skill_key(s) == _skill_key(change.suggested or '')), None)
            if target is None:
                return None, 'unknown_target'
            kept = [s for s in skills if s != target]
            return {**out, 'original': ', '.join(skills), 'suggested': ', '.join(kept), 'skill': target}, None
        if where['section'] == 'experience' and 'bulletIndex' in where:
            original, reason = bullet_text()
            if not original:
                return None, reason
            role = _one(content.get('experience'), where['experienceId'])
            bullets = [b for b in role['bullets'] if isinstance(b, str)] if role else []
            # Removing a bullet removes a fact. Only a bullet that really repeats another one may go.
            if len(bullets) != len(role['bullets']) or not _repeats_another(role['bullets'], where['bulletIndex']):
                return None, 'delete_not_duplicate'
            return {**out, 'original': original, 'suggested': ''}, None
        return None, 'delete_target_not_supported'

    if change.op == 'reorder':
        if where['section'] == 'skills':
            if len(skills) < 2:
                return None, 'bad_permutation'
            # Follow the model's order for the skills it named and that exist; everything it left out keeps its
            # original relative order at the end. The result is always the same skills, so nothing is added or lost.
            remaining = list(skills)
            new: list[str] = []
            for key in (_skill_key(x) for x in change.order_skills):
                hit = next((s for s in remaining if _skill_key(s) == key), None)
                if hit is not None:
                    remaining.remove(hit)
                    new.append(hit)
            if not new:
                return None, 'bad_permutation'
            new += remaining
            if new == skills:
                return None, 'no_change'
            return {**out, 'original': ', '.join(skills), 'suggested': ', '.join(new), 'risk': 'auto'}, None
        if where['section'] == 'experience' and 'bulletIndex' not in where:
            role = _one(content.get('experience'), where['experienceId'])
            bullets = role.get('bullets') if role else None
            if not isinstance(bullets, list) or len(bullets) < 2 or not all(isinstance(b, str) for b in bullets):
                return None, 'unknown_target'
            # Same repair as skills: valid indices in the model's order, each once, then the ones it left out.
            order = list(dict.fromkeys(i for i in change.order_bullets if 0 <= i < len(bullets)))
            if not order:
                return None, 'bad_permutation'
            order += [i for i in range(len(bullets)) if i not in order]
            if order == list(range(len(bullets))):
                return None, 'no_change'
            return {**out, 'original': '\n'.join(bullets), 'suggested': '\n'.join(bullets[i] for i in order),
                    'order': order, 'risk': 'auto'}, None
        return None, 'reorder_target_not_supported'

    # add_skill: backed by a verbatim quotation from somewhere in the CV OTHER than the skills list or the summary.
    if where['section'] != 'skills':
        return None, 'add_skill_target_not_skills'
    skill = ' '.join((change.suggested or '').split())
    if not skill or len(skill) > 60:
        return None, 'empty_suggestion'
    if any(_skill_key(skill) == _skill_key(s) for s in skills):
        return None, 'already_listed'
    path = (change.evidence_path or '').strip().strip('[]')
    quote = change.evidence_quote or ''
    if not path or not quote or path.split('.')[0] in ('skills', 'summary', 'languages'):
        return None, 'no_evidence'
    resolved = resolve_citation(content, Citation(path=path, quote=quote))
    if resolved is None:
        return None, 'evidence_not_in_cv'
    if not _names(skill, quote):
        better = _field_naming(content, resolved, skill)
        if better is None:
            return None, 'evidence_does_not_name_skill'
        resolved, quote = better[0], better[1][:240]
    return {**out, 'original': ', '.join(skills), 'suggested': ', '.join([*skills, skill]), 'skill': skill,
            'evidence': {'path': resolved, 'quote': quote.strip()}}, None
