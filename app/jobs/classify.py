"""Decide whether a job is in Poland, which city, and whether it is entry level.

Pure functions (no I/O) so they are easy to test.
"""
import html
import re

# Polish city spellings (with and without diacritics, plus English names) -> one display name.
CITIES = {
    'Warsaw': r'warsaw|warszawa',
    'Kraków': r'krak[oó]w|cracow',
    'Wrocław': r'wroc[lł]aw',
    'Gdańsk': r'gda[nń]sk',
    'Gdynia': r'gdynia',
    'Sopot': r'sopot',
    'Tricity': r'tricity|tr[oó]jmiasto',
    'Poznań': r'pozna[nń]',
    'Łódź': r'[lł][oó]d[zź]',
    'Katowice': r'katowice',
    'Gliwice': r'gliwice',
    'Szczecin': r'szczecin',
    'Lublin': r'lublin',
    'Białystok': r'bia[lł]ystok',
    'Rzeszów': r'rzesz[oó]w',
    'Bydgoszcz': r'bydgoszcz',
    'Toruń': r'toru[nń]',
    'Olsztyn': r'olsztyn',
    'Opole': r'opole',
    'Kielce': r'kielce',
    'Bielsko-Biała': r'bielsko-bia[lł]a',
}
_CITY_RES = [(name, re.compile(rf'\b({pattern})\b', re.I)) for name, pattern in CITIES.items()]
_COUNTRY = re.compile(r'\b(poland|polska|polen|pologne)\b', re.I)

INTERN_TITLE = re.compile(
    r'\b(intern|interns|internship|trainee|apprentice|apprenticeship|working student|werkstudent|'
    r'sta[zż]|sta[zż]ysta|sta[zż]ystka|praktyk[ai]?|praktykant(ka)?|summer analyst|placement)\b', re.I)
ENTRY_TITLE = re.compile(
    r'\b(junior|jr\.?|entry[- ]level|graduate|grad|new grad|absolwent(ka)?|m[lł]odszy|m[lł]odsza|'
    r'student|associate consultant|early careers?|early talent)\b', re.I)
# Words that always mean "not entry level", even next to "junior".
SENIOR_TITLE = re.compile(
    r'\b(senior|sr\.?|lead|principal|staff|head|director|vp|chief|starszy|starsza|kierownik|dyrektor)\b', re.I)
# Job functions that usually need experience. They only block the weaker description signal,
# so "Junior Project Manager" still counts as entry level.
EXPERIENCED_TITLE = re.compile(r'\b(manager|architect|expert|ekspert|specialist|consultant|mid|regular)\b', re.I)
ENTRY_DESCRIPTION = re.compile(
    r'(status (ucznia lub )?studenta|student status|0\s*[-–]\s*1 (years?|rok)|no (prior )?experience '
    r'(is )?required|bez do[sś]wiadczenia|recent graduates?|fresh graduates?|final[- ]year student|'
    r'currently (enrolled|studying)|dla student[oó]w|program(me)? sta[zż]owy|graduate programme?)', re.I)

INTERNSHIP = 'internship'
ENTRY = 'entry'
OTHER = 'other'


def polish_city(location: str) -> str | None:
    """Return the display city for a Polish location, 'Poland' if only the country is named, else None."""
    for name, pattern in _CITY_RES:
        if pattern.search(location or ''):
            return name
    if _COUNTRY.search(location or ''):
        return 'Poland'
    return None


def classify_level(title: str, structured: str | None = None, description: str = '') -> tuple[str, str | None]:
    """Return (level, signal). `structured` is the level an API reports itself, which wins."""
    if structured in (INTERNSHIP, ENTRY):
        return structured, 'api'
    t = title or ''
    if INTERN_TITLE.search(t):
        return INTERNSHIP, 'title'
    if ENTRY_TITLE.search(t) and not SENIOR_TITLE.search(t):
        return ENTRY, 'title'
    if description and not SENIOR_TITLE.search(t) and not EXPERIENCED_TITLE.search(t) and ENTRY_DESCRIPTION.search(description):
        return ENTRY, 'description'
    return OTHER, None


def html_to_text(value: str | None) -> str:
    """Rough HTML -> plain text, keeping paragraph breaks. Good enough for matching and display."""
    if not value:
        return ''
    text = html.unescape(value)
    text = re.sub(r'(?i)<\s*(br|/p|/li|/h[1-6]|/div)\s*/?>', '\n', text)
    text = re.sub(r'(?i)<\s*li[^>]*>', '- ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()
