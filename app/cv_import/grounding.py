"""Source grounding for Smart CV Import: every value the model returns must be found in the PDF's own text.

The model's output is never trusted on its own. A value is kept only when it appears in the extracted text, compared
case-sensitively after normalizing whitespace, line breaks, ligatures, curly quotes, dash styles, bullet symbols and
words hyphenated across a line break (differences that come from PDF text extraction, not from wording). Words must
match whole: "Java" is not confirmed by "JavaScript".

Ownership. Each job, school or project is located at its own heading in the CV: the place where its title and
organization are written together on the same or adjacent lines (its dates and first line of content break ties).
Other mentions of the same company or title elsewhere do not count, and a shorter title inside a longer one ("Engineer"
within "Senior Engineer") belongs to the longer one, so two roles at one employer stay apart. An entry's region runs
from its heading to the next entry heading, the next section heading (Skills, Hobbies, References, ...) or a maximum
length, whichever comes first. A bullet point, note or description is kept only when it is written inside its own
entry's region.

Dates. A date belongs to the entry whose heading it is written at, judged in the visual layout whenever pypdf can
produce one (its lines and columns show which heading a date sits beside; reading order can merge a sidebar into a
heading's line): on the heading's own line(s), inside the heading's text or in a date-only column at or right of the
heading, or on a date-only line directly above or below the heading (a right-aligned date pushed onto its own line by
a narrow column or a wide font). A date-only column left of the heading (a sidebar year) never counts. A date line
between two headings is assigned only when the rest of the CV shows whether it prints dates after or before headings;
otherwise it is ambiguous and left out. Without a layout version, reading order is used: after the heading within
its region, or a date-only line directly before the heading; a block of date-only lines printed apart from the
entries is never assigned. Text before the first entry (a summary or profile) or in another section is never attached
to an entry. Anything that fails is left out and reported as a warning; nothing is corrected or filled in.

The text is checked in several forms: reading order and visual layout (pypdf's two modes, which differ for
multi-column CVs, for example right-aligned dates), each as extracted, with line-end hyphenation joined, and with
hyphens between letters ignored on both sides (a word hyphenated across a line). Hyphens next to digits always count.
A value passes when one form confirms it; ownership is always judged within a single form.
"""

import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from app.cv.content import unsafe_contact_link
from app.services.ai_cv_import import ImportedCv

HEADER_WINDOW = 120  # an entry's title, organization and dates are written within this many characters of each other
DATE_WINDOW = 200  # a date must be within this many characters after its own entry's heading...
DATE_BEFORE_HEADER = 30  # ...or immediately before it, for CVs that print the dates first
REGION_MAX = 3000  # an entry's text never extends further than this from its heading
CONTENT_WINDOW = 400  # an entry's first line of content starts within this many characters of its heading

_TYPOGRAPHY = str.maketrans({
    '‘': "'", '’': "'", '‚': "'", '‛': "'", '′': "'",
    '“': '"', '”': '"', '„': '"', '″': '"',
    '‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-', '―': '-', '−': '-',
    ' ': ' ', '­': '',
})
_MARKER = re.compile(r'^\s*(?:[-*•–—·▪●◦‣⁃■►➢]|\d{1,2}[.)])\s+')
_HYPHEN_BREAK = re.compile(r'(\w)([-‐­])[ \t]*\n\s*(\w)')
# Words that may appear in a line holding only dates: month names and "present"-like words (English and Polish).
_DATE_WORDS = re.compile(
    r'\b(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|'
    r'october|nov|november|dec|december|present|current|currently|now|today|ongoing|to|date|till|until|since|'
    r'sty|stycze[nń]|lut|luty|marz|marzec|kwi|kwiecie[nń]|maj|cze|czerwiec|lip|lipiec|sie|sierpie[nń]|wrz|'
    r'wrzesie[nń]|pa[zź]|pa[zź]dziernik|lis|listopad|gru|grudzie[nń]|obecnie|teraz|od|do)\b\.?', re.IGNORECASE)
_WORD_HYPHEN = re.compile(r'(?<=[^\W\d_])-(?=[^\W\d_])')  # a hyphen between two letters, never next to a digit
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_PHONE = re.compile(r'\+?\(?\d[\d\s().\-/]{5,}\d')
_LIST_SEPARATORS = re.compile(r'\s*(?:[,;|•·]|\s/\s)\s*')
_PRESENT = {'present', 'current', 'currently', 'now', 'ongoing', 'today', 'to date', 'till date', 'to present'}
_MONTHS = {name: number for number, names in enumerate((
    ('jan', 'january'), ('feb', 'february'), ('mar', 'march'), ('apr', 'april'), ('may',), ('jun', 'june'),
    ('jul', 'july'), ('aug', 'august'), ('sep', 'sept', 'september'), ('oct', 'october'), ('nov', 'november'),
    ('dec', 'december')), start=1) for name in names}

# Section headings that end the previous entry's region (English and Polish). A line counts as a heading when it is
# one of these, optionally followed by ":" and inline content ("Skills: Python, Go").
_HEADINGS = frozenset((
    'summary', 'profile', 'professional summary', 'about', 'about me', 'objective', 'career objective',
    'personal statement', 'contact', 'contact details', 'experience', 'work experience', 'professional experience',
    'relevant experience', 'employment', 'employment history', 'work history', 'career history', 'education',
    'academic background', 'qualifications', 'projects', 'personal projects', 'selected projects', 'skills',
    'technical skills', 'key skills', 'core skills', 'soft skills', 'competencies', 'core competencies', 'tools',
    'technologies', 'tech stack', 'languages', 'language skills', 'certifications', 'certificates', 'licenses',
    'courses', 'training', 'awards', 'honors', 'honours', 'achievements', 'publications', 'volunteering',
    'volunteer experience', 'activities', 'extracurricular activities', 'interests', 'hobbies',
    'hobbies and interests', 'references', 'additional information', 'other',
    'podsumowanie', 'profil', 'o mnie', 'cel zawodowy', 'kontakt', 'doświadczenie', 'doświadczenie zawodowe',
    'historia zatrudnienia', 'wykształcenie', 'edukacja', 'projekty', 'umiejętności', 'kompetencje', 'technologie',
    'języki', 'języki obce', 'certyfikaty', 'kursy', 'szkolenia', 'nagrody', 'osiągnięcia', 'publikacje',
    'wolontariat', 'aktywności', 'zainteresowania', 'hobby', 'referencje', 'informacje dodatkowe',
))

# CvContent field limits (app/cv/content.py). Over-long values are reported, never cut.
_LIMIT = {'label': 300, 'short': 128, 'detail': 4000, 'bullet': 2000, 'summary': 8000, 'email': 320, 'link': 2048,
          'skill': 200}
MAX_ENTRIES, MAX_BULLETS, MAX_SKILLS, MAX_LINKS = 100, 40, 200, 24

MESSAGES = {
    'not_in_cv': 'Not found in your CV, so it was left out.',
    'entry_not_in_cv': 'This entry could not be found in your CV, so it was left out.',
    'date_not_near_entry': 'This date was not found next to this entry in your CV, so it was left out.',
    'date_ambiguous': 'This date could belong to more than one entry in your CV, so it was left out.',
    'under_other_entry': 'This text appears under a different entry in your CV, so it was left out.',
    'outside_entry': 'This text is not part of this entry in your CV, so it was left out.',
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


def _only_dates(text: str) -> bool:
    """Whether a line or column holds nothing but dates ("Jan 2021 - Present", "09/2018 - 06/2020", "2019")."""
    rest = _DATE_WORDS.sub(' ', fold(text))
    return any(ch.isdigit() for ch in text) and not any(ch.isalnum() and not ch.isdigit() for ch in rest)


def _heading_text_with_dates(part: str, names: tuple[str, ...]) -> bool:
    """Whether a line's column holds dates plus words of the entry's own heading (a heading that wrapped onto the
    date's line, such as "Technology - 2015 - 2019"), and nothing else."""
    words = re.findall(r'[^\W\d_]+', _DATE_WORDS.sub(' ', part))
    own = {word for name in names for word in re.findall(r'[^\W\d_]+', name)}
    return any(ch.isdigit() for ch in part) and bool(words) and all(word in own for word in words)


def _is_heading(line: str) -> bool:
    text = _MARKER.sub('', line).strip().casefold()
    if text.rstrip(':').strip() in _HEADINGS:
        return True
    head, colon, _ = text.partition(':')
    return bool(colon) and head.strip() in _HEADINGS


@dataclass
class Form:
    """One normalized version of the text: lines folded and joined by single spaces."""
    text: str
    headings: list[int] = field(default_factory=list)  # where section-heading lines start
    lines: list[int] = field(default_factory=list)  # where every line starts
    word_hyphens_ignored: bool = False  # this form, and needles searched in it, drop hyphens between letters
    layout: bool = False  # visual-line text, where a sidebar can share lines with the main column
    raw_lines: list[str] = field(default_factory=list)  # each line before folding (keeps column gaps)

    def line_of(self, position: int) -> int:
        return bisect_right(self.lines, position) - 1

    def segments(self, line: int) -> list[tuple[int, str]]:
        """The parts of a line separated by wide gaps, with the column each starts at: in visual-layout text, the
        page columns sharing that line, from left to right."""
        return [(match.start(), match.group(0)) for match in re.finditer(r'\S+(?:\s{1,2}\S+)*', self.raw_lines[line])]


def _form(raw: str, *, ignore_word_hyphens: bool = False) -> Form:
    parts, headings, lines, raw_lines, position = [], [], [], [], 0
    for line in raw.splitlines():
        folded = fold(line)
        if ignore_word_hyphens:
            folded = _WORD_HYPHEN.sub('', folded)
        if not folded:
            continue
        if parts:
            position += 1  # the joining space
        lines.append(position)
        raw_lines.append(line)
        if _is_heading(folded):
            headings.append(position)
        parts.append(folded)
        position += len(folded)
    return Form(' '.join(parts), headings, lines, ignore_word_hyphens, raw_lines=raw_lines)


def _forms(raw: str, *, layout: bool = False) -> list[Form]:
    """The text as extracted; with line-end hyphenation joined ("com-/piler" -> "compiler"); and the joined text with
    hyphens between letters ignored on both sides, so a word hyphenated in the CV ("machine-/independent") matches
    however the line break fell. Hyphens next to digits (ranges such as "10-20%") always count."""
    joined = _HYPHEN_BREAK.sub(r'\1\3', raw)
    unique, seen = [], set()
    for form in (_form(raw), _form(joined), _form(joined, ignore_word_hyphens=True)):
        form.layout = layout
        if (form.text, form.word_hyphens_ignored) not in seen:
            seen.add((form.text, form.word_hyphens_ignored))
            unique.append(form)
    return unique


class Source:
    """The extracted text in comparable forms. Positions are only compared within one form."""

    def __init__(self, text: str, layout_text: str | None = None):
        raws = [text, *([layout_text] if layout_text and layout_text != text else [])]
        self.forms = _forms(text) + (_forms(layout_text, layout=True) if len(raws) > 1 else [])
        self.compact = re.sub(r'\s+', '', self.forms[0].text) if self.forms else ''  # links and emails across lines
        self.phones = [match.group(0) for raw in raws for match in _PHONE.finditer(raw)]

    def positions(self, needle: str, form: int) -> list[int]:
        """Whole-word occurrences of `needle` in one form."""
        if self.forms[form].word_hyphens_ignored:
            needle = _WORD_HYPHEN.sub('', needle)
        if not needle or not any(ch.isalnum() for ch in needle):
            return []
        haystack, found, start = self.forms[form].text, [], 0
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


@dataclass
class Entry:
    section: str
    key: str
    item: Any
    main: str  # verified title / degree ('' for projects)
    org: str  # verified company / school / project name
    hints: list[str]  # the model's dates and first line of content, used only to locate the heading
    dates: list[str] = field(default_factory=list)  # the model's dates, used only to tell how this CV places dates


class Grounder:
    def __init__(self, text: str, layout_text: str | None = None):
        self.source = Source(text, layout_text)
        self.warnings: list[dict[str, str]] = []
        self.headers: list[dict[str, int]] = [{} for _ in self.source.forms]  # per form: entry key -> heading position
        self.heading_lines: list[dict[str, tuple[int, int]]] = [{} for _ in self.source.forms]  # first, last line
        self.heading_columns: list[dict[str, int]] = [{} for _ in self.source.forms]  # layout: where a heading starts
        self.entry_dates: dict[str, list[str]] = {}
        self.entry_names: dict[str, tuple[str, ...]] = {}

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
        kept = []
        for part in (part for part in _LIST_SEPARATORS.split(value) if part):
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

    # --- entry headings -----------------------------------------------------------------------------------------
    def locate(self, entries: list[Entry]) -> None:
        """Find each entry's own heading in every form (see the module docstring)."""
        self.entry_dates = {entry.key: entry.dates for entry in entries}
        self.entry_names = {entry.key: tuple(name for name in (entry.main, entry.org) if name) for entry in entries}
        names = {name for entry in entries for name in (entry.main, entry.org) if name}
        for form in range(len(self.source.forms)):
            occurrences = {name: self.source.positions(name, form) for name in names}
            line_of = self.source.forms[form].line_of

            def standalone(name: str, occurrences: dict[str, list[int]] = occurrences) -> list[int]:
                # An occurrence inside a longer entry name ("Engineer" within "Senior Engineer") is that entry's.
                spans = [(start, start + len(other)) for other in names if len(other) > len(name) and name in other
                         for start in occurrences[other]]
                own = [p for p in occurrences[name] if not any(s <= p and p + len(name) <= e for s, e in spans)]
                return own or occurrences[name]

            claimed: set[int] = set()
            previous: dict[str, int] = {}
            for entry in entries:
                primary = entry.org or entry.main
                secondary = entry.main if entry.org and entry.main else ''
                candidates = []
                for position in standalone(primary):
                    heading, last, score = position, position + len(primary) - 1, 0
                    if secondary:
                        # The title and organization of one heading are on the same line or adjacent lines.
                        near = [p for p in standalone(secondary)
                                if abs(line_of(p) - line_of(position)) <= 1 and abs(p - position) <= HEADER_WINDOW]
                        if near:
                            closest = min(near, key=lambda p: abs(p - position))
                            heading, score = min(position, closest), 2
                            last = max(position + len(primary), closest + len(secondary)) - 1
                    for hint in entry.hints:
                        spots = self.source.positions(hint, form)
                        if any(abs(spot - heading) <= HEADER_WINDOW for spot in spots):
                            score += 1
                    candidates.append((score, heading, last))
                candidates = [c for c in candidates if c[1] not in claimed]
                if not candidates:
                    continue
                best = max(c[0] for c in candidates)
                tops = sorted((c[1], c[2]) for c in candidates if c[0] == best)
                after = [t for t in tops if t[0] > previous.get(entry.section, -1)]
                heading, last = (after or tops)[0]
                claimed.add(heading)
                previous[entry.section] = heading
                self.headers[form][entry.key] = heading
                self.heading_lines[form][entry.key] = (line_of(heading), line_of(last))
                if self.source.forms[form].layout:
                    columns = [column for line in range(line_of(heading), line_of(last) + 1)
                               for column, part in self.source.forms[form].segments(line)
                               if any(name in fold(part) for name in (entry.main, entry.org) if name)]
                    if columns:
                        self.heading_columns[form][entry.key] = min(columns)

    def _region(self, form: int, key: str) -> tuple[int, int] | None:
        heading = self.headers[form].get(key)
        if heading is None:
            return None
        ends = [h for h in self.headers[form].values() if h > heading]
        ends += [h for h in self.source.forms[form].headings if h > heading]
        return heading, min([heading + REGION_MAX, *ends])

    def _owner(self, form: int, position: int) -> str | None:
        for key in self.headers[form]:
            region = self._region(form, key)
            if region and region[0] <= position < region[1]:
                return key
        return None

    def _occurrences(self, value: str) -> list[tuple[int, int]]:
        needle = clean(value)
        return [(form, position) for form in range(len(self.source.forms))
                for position in self.source.positions(needle, form)]

    def _date_lines(self, form: int) -> set[int]:
        """Lines holding a date written for an entry: date-only lines in reading order; in the visual layout, lines
        with a date-only column at or right of where entry headings start (a sidebar year does not count)."""
        source = self.source.forms[form]
        if not source.layout:
            return {line for line in range(len(source.raw_lines)) if _only_dates(source.raw_lines[line])}
        left = min(self.heading_columns[form].values(), default=0)
        return {line for line in range(len(source.raw_lines))
                if any(_only_dates(part) and column >= left - 1 for column, part in source.segments(line))}

    def _convention(self, form: int) -> str | None:
        """How this CV places dates that are on their own line, judged from the text alone: 'after' each heading,
        'before' it, or None when the CV shows neither or both (then a date between two headings is ambiguous)."""
        heading_lines = {line for first, last in self.heading_lines[form].values() for line in range(first, last + 1)}
        after = before = False
        for line in self._date_lines(form):
            follows, precedes = line - 1 in heading_lines, line + 1 in heading_lines
            after = after or (follows and not precedes)
            before = before or (precedes and not follows and line - 1 not in self._date_lines(form))
        return 'after' if after and not before else 'before' if before and not after else None

    def _date_candidates(self, form: int, position: int, needle: str) -> set[str]:
        """The entries a date written at `position` can belong to. One entry: it belongs there. Several: the CV is
        ambiguous and the date is left out. None: it is not written at any entry's heading."""
        source = self.source.forms[form]
        line = source.line_of(position)
        if source.layout:
            # Visual lines. On a heading's own line(s) the date must be in the heading's text or in a date-only
            # column at or right of the heading (never a sidebar on the left). A date-only line directly above or
            # below a heading also counts (a right-aligned date that a narrow column pushed onto its own line).
            parts = [(column, fold(part)) for column, part in source.segments(line)]
            if source.word_hyphens_ignored:
                parts = [(column, _WORD_HYPHEN.sub('', part)) for column, part in parts]
            holding = [(column, part) for column, part in parts if needle in part]
            above, below = set(), set()
            for key, (first, last) in self.heading_lines[form].items():
                start = self.heading_columns[form].get(key, 0)
                dates_here = any(_only_dates(part) and column >= start - 1 for column, part in holding)
                if first <= line <= last:
                    names = self.entry_names.get(key, ())
                    if dates_here or any(any(name in part for name in names) or _heading_text_with_dates(part, names)
                                         for _, part in holding):
                        return {key}
                elif line == last + 1 and dates_here:
                    below.add(key)
                elif line == first - 1 and dates_here:
                    above.add(key)
        else:
            # Reading order: after a heading (inside its region, within DATE_WINDOW), or a date-only line directly
            # before a heading. A run of date-only lines (dates printed apart from their entries) is never used.
            raw = source.raw_lines
            detached = (line > 0 and _only_dates(raw[line - 1])) or (line + 1 < len(raw) and _only_dates(raw[line + 1]))
            if _only_dates(raw[line]) and detached:
                return set()
            below, above = set(), set()
            owner = self._owner(form, position)
            if owner is not None and position < self.headers[form][owner] + DATE_WINDOW:
                below.add(owner)
            following = sorted((h, k) for k, h in self.headers[form].items() if h > position)
            if following and following[0][0] - position <= DATE_BEFORE_HEADER and _only_dates(raw[line]):
                above.add(following[0][1])
        if not below or not above or below == above:
            return below | above
        convention = self._convention(form)  # between two headings: decide only from how this CV places dates
        return below if convention == 'after' else above if convention == 'before' else below | above

    def date(self, field: str, key: str, value: str, *, end: bool) -> str:
        """A date written at this entry's heading. 'Present' and similar become '' (an ongoing entry)."""
        value = clean(value)
        if not value:
            return ''
        occurrences = self._occurrences(value)
        if not occurrences:
            self.warn(field, value, 'not_in_cv')
            return ''
        # Judged in the visual layout whenever it exists: there a date's line and column show which heading it sits
        # beside, while reading order can merge a sidebar into a heading's line. Reading order is the fallback.
        if any(self.source.forms[form].layout for form, _ in occurrences):
            occurrences = [(form, position) for form, position in occurrences if self.source.forms[form].layout]
        elif any(form.layout for form in self.source.forms):
            occurrences = []  # the layout exists but does not contain this date
        ambiguous = False
        for form, position in occurrences:
            needle = _WORD_HYPHEN.sub('', value) if self.source.forms[form].word_hyphens_ignored else value
            candidates = self._date_candidates(form, position, needle)
            if candidates == {key}:
                break
            ambiguous = ambiguous or key in candidates
        else:
            self.warn(field, value, 'date_ambiguous' if ambiguous else 'date_not_near_entry')
            return ''
        if end and value.casefold() in _PRESENT:
            return ''
        return normalize_date(value)

    def belongs(self, field: str, key: str, value: str, limit: str) -> str:
        """Text written inside its own entry's region (a bullet point, description or note)."""
        value = clean(value)
        if not value:
            return ''
        occurrences = self._occurrences(value)
        if not occurrences:
            self.warn(field, value, 'not_in_cv')
            return ''
        # Judged in reading order whenever the text appears there; the visual layout can put a sidebar's words on the
        # same lines as a job, so it only decides when reading order has no occurrence.
        reading = [(form, position) for form, position in occurrences if not self.source.forms[form].layout]
        owners = {self._owner(form, position) for form, position in (reading or occurrences)}
        if key not in owners:
            self.warn(field, value, 'under_other_entry' if owners - {None} else 'outside_entry')
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


def ground(imported: ImportedCv, text: str, layout_text: str | None = None
           ) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """CvContent-shaped data containing only values found in the CV text, the warnings, and the fields left empty."""
    g = Grounder(text, layout_text)

    entries: list[Entry] = []
    for section, items in (('experience', imported.experience), ('education', imported.education),
                           ('projects', imported.projects)):
        for index, item in enumerate(_cap(g, section, list(items), MAX_ENTRIES)):
            key = f'{section}[{index}]'
            if section == 'experience':
                main = g.text(f'{key}.title', item.title, 'label')
                org = g.text(f'{key}.company', item.company, 'label')
                label = ' · '.join(part for part in (item.title.strip(), item.company.strip()) if part)
                hints = [item.start, item.end, *item.bullets[:1]]
            elif section == 'education':
                main = g.text(f'{key}.degree', item.degree, 'label')
                org = g.text(f'{key}.school', item.school, 'label')
                label = ' · '.join(part for part in (item.degree.strip(), item.school.strip()) if part)
                hints = [item.start, item.end, item.note]
            else:
                main, org = '', g.text(f'{key}.name', item.name, 'label')
                label = item.name.strip()
                hints = [item.tech, item.description]
            if not main and not org:
                if label:
                    g.warn(key, label, 'entry_not_in_cv')
                continue
            dates = [clean(d) for d in (item.start, item.end) if clean(d)] if section != 'projects' else []
            entries.append(Entry(section, key, item, main, org, [clean(h) for h in hints if clean(h)], dates))
    g.locate(entries)

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
    for entry in entries:
        section, key, item = entry.section, entry.key, entry.item
        number = counters[section] = counters[section] + 1
        if section == 'experience':
            bullets = [kept for index, bullet in enumerate(item.bullets)
                       if (kept := g.belongs(f'{key}.bullets[{index}]', key, bullet, 'bullet'))]
            content['experience'].append({
                'id': f'import-experience-{number}', 'title': entry.main, 'company': entry.org,
                'location': g.text(f'{key}.location', item.location, 'label'),
                'start': g.date(f'{key}.start', key, item.start, end=False),
                'end': g.date(f'{key}.end', key, item.end, end=True),
                'bullets': _cap(g, f'{key}.bullets', bullets, MAX_BULLETS),
            })
        elif section == 'education':
            content['education'].append({
                'id': f'import-education-{number}', 'school': entry.org, 'degree': entry.main,
                'start': g.date(f'{key}.start', key, item.start, end=False),
                'end': g.date(f'{key}.end', key, item.end, end=True),
                'note': g.belongs(f'{key}.note', key, item.note, 'detail'),
            })
        else:
            content['projects'].append({
                'id': f'import-project-{number}', 'name': entry.org,
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
