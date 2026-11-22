"""Source grounding for Smart CV Import: every value the model returns must be found in the PDF's own text.

The model's output is never trusted on its own. A value is kept only when it appears in the extracted text, compared
case-sensitively after normalizing whitespace, line breaks, ligatures, curly quotes, dash styles, bullet symbols and
words hyphenated across a line break (differences that come from PDF text extraction, not from wording). Words must
match whole: "Java" is not confirmed by "JavaScript". Dates and bullet points must also sit next to their own entry,
so a date or achievement is never moved from one job to another. Anything that fails is left out and reported as a
warning; nothing is corrected or filled in.
"""

import re
import unicodedata
from typing import Any

from app.cv.content import unsafe_contact_link
from app.services.ai_cv_import import ImportedCv

DATE_WINDOW = 200  # a date must be within this many characters after its own entry's title or organization
DATE_BEFORE_HEADER = 30  # ...or immediately before it, for CVs that print the dates first

_TYPOGRAPHY = str.maketrans({
    '‘': "'", '’': "'", '‚': "'", '‛': "'", '′': "'",
    '“': '"', '”': '"', '„': '"', '″': '"',
    '‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-', '―': '-', '−': '-',
    ' ': ' ', '­': '',
})
_MARKER = re.compile(r'^\s*(?:[-*•–—·▪●◦‣⁃■►➢]|\d{1,2}[.)])\s+')
_HYPHEN_BREAK = re.compile(r'(\w)[-­‐]\s*\n\s*(\w)')
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_PHONE = re.compile(r'\+?\(?\d[\d\s().\-/]{5,}\d')
_LIST_SEPARATORS = re.compile(r'\s*(?:[,;|•·]|\s/\s)\s*')
_PRESENT = {'present', 'current', 'currently', 'now', 'ongoing', 'today', 'to date', 'till date', 'to present'}
_MONTHS = {name: number for number, names in enumerate((
    ('jan', 'january'), ('feb', 'february'), ('mar', 'march'), ('apr', 'april'), ('may',), ('jun', 'june'),
    ('jul', 'july'), ('aug', 'august'), ('sep', 'sept', 'september'), ('oct', 'october'), ('nov', 'november'),
    ('dec', 'december')), start=1) for name in names}

# CvContent field limits (app/cv/content.py). Over-long values are reported, never cut.
_LIMIT = {'label': 300, 'short': 128, 'detail': 4000, 'bullet': 2000, 'summary': 8000, 'email': 320, 'link': 2048,
          'skill': 200}
MAX_ENTRIES, MAX_BULLETS, MAX_SKILLS, MAX_LINKS = 100, 40, 200, 24

MESSAGES = {
    'not_in_cv': 'Not found in your CV, so it was left out.',
    'entry_not_in_cv': 'This entry could not be found in your CV, so it was left out.',
    'date_not_near_entry': 'This date was not found next to this entry in your CV, so it was left out.',
    'under_other_entry': 'This text appears under a different entry in your CV, so it was left out.',
    'unsafe_link': 'This link uses an unsafe address, so it was left out.',
    'invalid_email': 'This is not a valid email address, so it was left out.',
    'too_long': 'This text is longer than FlowJob supports, so it was left out.',
    'over_limit': 'FlowJob supports fewer items here, so the rest were left out.',
}

REQUIRED_FOR_REVIEW = ('fullName', 'email', 'phone', 'location', 'summary', 'experience', 'education', 'skills')


def fold(text: str) -> str:
    """Extraction-only differences removed; wording, spelling and capitalization kept."""
    return ' '.join(unicodedata.normalize('NFKC', text).translate(_TYPOGRAPHY).split())


def clean(value: str) -> str:
    return _MARKER.sub('', fold(value or '')).strip()


def _short(value: str) -> str:
    return value if len(value) <= 160 else value[:157] + '...'


class Source:
    """The extracted text in comparable forms. Positions are only compared within one form."""

    def __init__(self, text: str):
        forms = [fold(text), fold(_HYPHEN_BREAK.sub(r'\1\2', text))]
        self.forms = list(dict.fromkeys(forms))
        self.compact = re.sub(r'\s+', '', self.forms[0])  # links and emails broken across lines
        self.phones = [match.group(0) for match in _PHONE.finditer(text)]

    def positions(self, needle: str, form: int) -> list[int]:
        """Whole-word occurrences of `needle` in one form."""
        if not needle or not any(ch.isalnum() for ch in needle):
            return []
        haystack, found, start = self.forms[form], [], 0
        while (index := haystack.find(needle, start)) != -1:
            before = haystack[index - 1] if index else ' '
            end = index + len(needle)
            after = haystack[end] if end < len(haystack) else ' '
            if not (needle[0].isalnum() and before.isalnum()) and not (needle[-1].isalnum() and after.isalnum()):
                found.append(index)
            start = index + 1
        return found

    def contains(self, value: str) -> bool:
        needle = clean(value)
        return any(self.positions(needle, form) for form in range(len(self.forms)))


class Grounder:
    def __init__(self, text: str):
        self.source = Source(text)
        self.warnings: list[dict[str, str]] = []
        # Entry anchors per text form: (position, entry key, is the organization/name anchor)
        self.anchors: list[list[tuple[int, str, bool]]] = [[] for _ in self.source.forms]

    # --- reporting -----------------------------------------------------------------------------------------------
    def warn(self, field: str, value: str, reason: str) -> None:
        self.warnings.append({'field': field, 'value': _short(value), 'reason': reason, 'message': MESSAGES[reason]})

    # --- plain values ------------------------------------------------------------------------------------------
    def text(self, field: str, value: str, limit: str) -> str:
        """The value if it appears in the CV, else '' with a warning."""
        value = clean(value)
        if not value:
            return ''
        if not self.source.contains(value):
            self.warn(field, value, 'not_in_cv')
            return ''
        if len(value) > _LIMIT[limit]:
            self.warn(field, value, 'too_long')
            return ''
        return value

    def listed(self, field: str, value: str, limit: str) -> str:
        """A list written in one field (technologies, languages): kept whole, or item by item when only the
        separators differ from the CV. Items not in the CV are reported."""
        value = clean(value)
        if not value:
            return ''
        if self.source.contains(value):
            return value if len(value) <= _LIMIT[limit] else self.text(field, value, limit)
        parts = [part for part in _LIST_SEPARATORS.split(value) if part]
        kept = []
        for part in parts:
            if self.source.contains(part):
                kept.append(part)
            else:
                self.warn(field, part, 'not_in_cv')
        joined = ', '.join(kept)
        if len(joined) > _LIMIT[limit]:
            self.warn(field, joined, 'too_long')
            return ''
        return joined

    def email(self, value: str) -> str:
        value = clean(value)
        if not value:
            return ''
        if not _EMAIL.match(value):
            self.warn('email', value, 'invalid_email')
            return ''
        if not (self.source.contains(value) or value in self.source.compact):
            self.warn('email', value, 'not_in_cv')
            return ''
        return value if len(value) <= _LIMIT['email'] else ''

    def phone(self, value: str) -> str:
        """Kept as the CV writes it: the model may not reformat a number."""
        value = clean(value)
        if not value:
            return ''
        digits = re.sub(r'[^\d+]', '', value)
        for written in self.source.phones:
            if re.sub(r'[^\d+]', '', written) == digits and len(digits) >= 6:
                written = fold(written)
                return written if len(written) <= _LIMIT['short'] else ''
        self.warn('phone', value, 'not_in_cv')
        return ''

    def link(self, field: str, value: str) -> str:
        value = clean(value)
        if not value:
            return ''
        if unsafe_contact_link(value):
            self.warn(field, value, 'unsafe_link')
            return ''
        if not (self.source.contains(value) or re.sub(r'\s+', '', value) in self.source.compact):
            self.warn(field, value, 'not_in_cv')
            return ''
        if len(value) > _LIMIT['link']:
            self.warn(field, value, 'too_long')
            return ''
        return value

    # --- entries and what belongs to them --------------------------------------------------------------------
    def add_anchor(self, key: str, value: str, organization: bool) -> None:
        needle = clean(value)
        for form in range(len(self.source.forms)):
            self.anchors[form].extend((position, key, organization) for position in self.source.positions(needle, form))

    def _occurrences(self, value: str) -> list[tuple[int, int]]:
        needle = clean(value)
        return [(form, position) for form in range(len(self.source.forms))
                for position in self.source.positions(needle, form)]

    def _closest(self, form: int, position: int, *, before: bool, organizations_only: bool
                 ) -> tuple[int, set[str]] | None:
        """Distance to the closest anchor before (or after) `position`, and the entries it belongs to (several
        entries can share one, for example two roles at the same company)."""
        candidates = [(abs(position - anchor), key) for anchor, key, organization in self.anchors[form]
                      if (organization or not organizations_only) and ((anchor <= position) == before)]
        if not candidates:
            return None
        distance = min(gap for gap, _ in candidates)
        return distance, {key for gap, key in candidates if gap == distance}

    def _date_owners(self, form: int, position: int) -> set[str]:
        """The entry a date is written for: the entry heading just before it, or one starting right after it."""
        before = self._closest(form, position, before=True, organizations_only=False)
        after = self._closest(form, position, before=False, organizations_only=False)
        if after and after[0] <= DATE_BEFORE_HEADER and (before is None or after[0] < before[0]):
            return after[1]
        if before and before[0] <= DATE_WINDOW:
            return before[1]
        return set()

    def date(self, field: str, key: str, value: str, *, end: bool) -> str:
        """A date written next to this entry. 'Present' and similar become '' (an ongoing entry)."""
        value = clean(value)
        if not value:
            return ''
        occurrences = self._occurrences(value)
        if not occurrences:
            self.warn(field, value, 'not_in_cv')
            return ''
        if not any(key in self._date_owners(form, position) for form, position in occurrences):
            self.warn(field, value, 'date_not_near_entry')
            return ''
        if end and value.casefold() in _PRESENT:
            return ''
        return normalize_date(value)

    def belongs(self, field: str, key: str, value: str, limit: str) -> str:
        """Text written under its own entry (a bullet, description or note)."""
        value = clean(value)
        if not value:
            return ''
        occurrences = self._occurrences(value)
        if not occurrences:
            self.warn(field, value, 'not_in_cv')
            return ''
        organizations = any(organization for form in self.anchors for _, _, organization in form)
        for form, position in occurrences:
            closest = self._closest(form, position, before=True, organizations_only=organizations)
            if closest is None or key in closest[1]:
                break
        else:
            self.warn(field, value, 'under_other_entry')
            return ''
        if len(value) > _LIMIT[limit]:
            self.warn(field, value, 'too_long')
            return ''
        return value


def normalize_date(value: str) -> str:
    """'Jan 2020', '01/2020', '2020-01' and similar become '2020-01' (the CV Builder's month format). Anything else,
    such as a year alone or 'Summer 2019', is kept exactly as written."""
    text = value.strip()
    if match := re.fullmatch(r'(\d{4})-(\d{1,2})', text):
        year, month = int(match.group(1)), int(match.group(2))
    elif match := re.fullmatch(r'(\d{1,2})\s*[./]\s*(\d{4})', text):
        month, year = int(match.group(1)), int(match.group(2))
    elif match := re.fullmatch(r'(\d{4})\s*/\s*(\d{1,2})', text):
        year, month = int(match.group(1)), int(match.group(2))
    elif match := re.fullmatch(r'([A-Za-z]+)\.?,?\s+(\d{4})', text):
        month, year = _MONTHS.get(match.group(1).casefold(), 0), int(match.group(2))
    else:
        return text
    return f'{year:04d}-{month:02d}' if 1 <= month <= 12 and 1900 <= year <= 2100 else text


def _cap[T](g: Grounder, field: str, items: list[T], limit: int) -> list[T]:
    if len(items) > limit:
        g.warn(field, f'{len(items) - limit} more', 'over_limit')
    return items[:limit]


def ground(imported: ImportedCv, text: str) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """CvContent-shaped data containing only values found in `text`, the warnings, and the fields left empty."""
    g = Grounder(text)

    # Anchors first, so dates and bullets can be checked against their own entry.
    entries: list[tuple[str, str, Any, str, str]] = []  # (section, key, raw entry, verified main, verified org)
    for section, items in (('experience', imported.experience), ('education', imported.education),
                           ('projects', imported.projects)):
        for index, item in enumerate(_cap(g, section, list(items), MAX_ENTRIES)):
            key = f'{section}[{index}]'
            if section == 'experience':
                main = g.text(f'{key}.title', item.title, 'label')
                org = g.text(f'{key}.company', item.company, 'label')
                label = ' · '.join(part for part in (item.title.strip(), item.company.strip()) if part)
            elif section == 'education':
                main = g.text(f'{key}.degree', item.degree, 'label')
                org = g.text(f'{key}.school', item.school, 'label')
                label = ' · '.join(part for part in (item.degree.strip(), item.school.strip()) if part)
            else:
                main, org = '', g.text(f'{key}.name', item.name, 'label')
                label = item.name.strip()
            if not main and not org:
                if label:
                    g.warn(key, label, 'entry_not_in_cv')
                continue
            if org:
                g.add_anchor(key, org, organization=True)
            if main:
                g.add_anchor(key, main, organization=not org)
            entries.append((section, key, item, main, org))

    content: dict[str, Any] = {
        'fullName': g.text('fullName', imported.full_name, 'label'),
        'email': g.email(imported.email),
        'phone': g.phone(imported.phone),
        'location': g.text('location', imported.location, 'label'),
        'links': [],
        'summary': g.text('summary', imported.summary, 'summary'),
        'experience': [],
        'education': [],
        'projects': [],
        'skills': [],
        'languages': g.listed('languages', imported.languages, 'detail'),
    }
    for index, link in enumerate(imported.links):
        if (kept := g.link(f'links[{index}]', link)) and kept not in content['links']:
            content['links'].append(kept)
    content['links'] = _cap(g, 'links', content['links'], MAX_LINKS)

    counters = {'experience': 0, 'education': 0, 'projects': 0}
    for section, key, item, main, org in entries:
        number = counters[section] = counters[section] + 1
        if section == 'experience':
            bullets = [kept for index, bullet in enumerate(item.bullets)
                       if (kept := g.belongs(f'{key}.bullets[{index}]', key, bullet, 'bullet'))]
            content['experience'].append({
                'id': f'import-experience-{number}', 'title': main, 'company': org,
                'location': g.text(f'{key}.location', item.location, 'label'),
                'start': g.date(f'{key}.start', key, item.start, end=False),
                'end': g.date(f'{key}.end', key, item.end, end=True),
                'bullets': _cap(g, f'{key}.bullets', bullets, MAX_BULLETS),
            })
        elif section == 'education':
            content['education'].append({
                'id': f'import-education-{number}', 'school': org, 'degree': main,
                'start': g.date(f'{key}.start', key, item.start, end=False),
                'end': g.date(f'{key}.end', key, item.end, end=True),
                'note': g.belongs(f'{key}.note', key, item.note, 'detail'),
            })
        else:
            content['projects'].append({
                'id': f'import-project-{number}', 'name': org,
                'tech': g.listed(f'{key}.tech', item.tech, 'detail'),
                'description': g.belongs(f'{key}.description', key, item.description, 'detail'),
            })

    seen: set[str] = set()
    for index, skill in enumerate(imported.skills):
        kept = g.text(f'skills[{index}]', skill, 'skill')
        if kept and kept.casefold() not in seen:
            seen.add(kept.casefold())
            content['skills'].append(kept)
    content['skills'] = _cap(g, 'skills', content['skills'], MAX_SKILLS)

    missing = [name for name in REQUIRED_FOR_REVIEW if not content[name]]
    return content, g.warnings, missing
