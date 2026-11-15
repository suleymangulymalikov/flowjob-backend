"""Candidate filtering (metadata only) and content minimization before AI analysis.

Nothing here persists text. Attachments are never decoded: parts with a filename or an attachment id are skipped,
and the Gmail client never requests attachment bodies.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

MAX_TEXT_CHARS = 4000

# Multilingual recruitment vocabulary (lower-case substrings). Broad on purpose: the AI decides relevance; this only
# keeps obviously unrelated mail (receipts, social notifications, personal mail) away from it.
KEYWORDS = (
    # English
    'application', 'applied', 'apply', 'candidate', 'candidacy', 'interview', 'assessment', 'recruit', 'hiring',
    'position', 'vacancy', 'job', 'career', 'job offer', 'offer letter', 'offer of employment', 'talent',
    'shortlist', 'onboarding', 'internship',
    'graduate programme', 'graduate program', 'coding challenge', 'test task', 'thank you for your interest',
    # German
    'bewerbung', 'beworben', 'vorstellungsgespräch', 'stelle', 'karriere', 'absage', 'zusage', 'praktikum',
    'einladung zum', 'personalabteilung', 'kandidat',
    # Polish
    'aplikacj', 'aplikowa', 'rekrutac', 'rozmow', 'kandydat', 'oferta', 'ofert', 'stanowisk', 'praca', 'pracy',
    'staż', 'praktyk', 'zgłoszeni',
    # French / Spanish / Italian / Portuguese / Dutch / Turkish
    'candidature', 'entretien', 'poste', 'recrutement', 'candidatura', 'solicitud', 'entrevista', 'puesto',
    'vacante', 'colloquio', 'posizione', 'selezione', 'vaga', 'sollicitatie', 'vacature', 'gesprek', 'başvuru',
    'mülakat', 'pozisyon',
)

ATS_DOMAINS = (
    'greenhouse.io', 'greenhouse-mail.io', 'lever.co', 'hire.lever.co', 'myworkday.com', 'myworkdayjobs.com',
    'workday.com', 'smartrecruiters.com', 'ashbyhq.com', 'icims.com', 'successfactors.com', 'sapsf.com',
    'taleo.net', 'oraclecloud.com', 'jobvite.com', 'personio.de', 'personio.com', 'recruitee.com',
    'teamtailor.com', 'teamtailor-mail.com', 'workable.com', 'breezy.hr', 'bamboohr.com', 'jazzhr.com',
    'pracuj.pl', 'erecruiter.pl', 'hrlink.pl', 'eightfold.ai', 'avature.net', 'phenompeople.com',
    'beamery.com', 'join.com', 'softgarden.de', 'rexx-systems.com', 'linkedin.com', 'indeed.com',
    'stepstone.de', 'xing.com', 'glassdoor.com', 'handshake.com', 'joinhandshake.com',
)

SKIP_LABELS = {'SPAM', 'TRASH', 'DRAFT', 'SENT', 'CHAT'}


def header(message: dict[str, Any], name: str) -> str:
    for h in (message.get('payload') or {}).get('headers') or []:
        if str(h.get('name', '')).lower() == name.lower():
            return str(h.get('value') or '')
    return ''


def internal_at(message: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(message.get('internalDate') or 0) / 1000, UTC)
    except (TypeError, ValueError, OverflowError):
        return datetime.fromtimestamp(0, UTC)


def sender_domain(message: dict[str, Any]) -> str:
    _, address = parseaddr(header(message, 'From'))
    return address.rsplit('@', 1)[-1].lower() if '@' in address else ''


def is_candidate(message: dict[str, Any], own_email: str) -> bool:
    """Metadata-only first stage: subject, snippet, sender and labels. Promotions are included on purpose."""
    if SKIP_LABELS & set(message.get('labelIds') or []):
        return False
    _, address = parseaddr(header(message, 'From'))
    if address and address.lower() == own_email.lower():
        return False
    domain = sender_domain(message)
    if any(domain == d or domain.endswith('.' + d) for d in ATS_DOMAINS):
        return True
    haystack = f"{header(message, 'Subject')} {message.get('snippet') or ''} {header(message, 'From')}".lower()
    return any(k in haystack for k in KEYWORDS)


# --- body extraction ---------------------------------------------------------------------------------------------

class _Text(HTMLParser):
    BLOCK = {'p', 'div', 'br', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'section', 'blockquote'}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.quote = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head', 'title'):
            self.skip += 1
        if tag == 'blockquote' or (tag == 'div' and any(v and 'gmail_quote' in v for _, v in attrs)):
            self.quote += 1
        if tag in self.BLOCK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head', 'title') and self.skip:
            self.skip -= 1
        if tag == 'blockquote' and self.quote:
            self.quote -= 1
        if tag in self.BLOCK:
            self.out.append('\n')

    def handle_data(self, data):
        if not self.skip and not self.quote:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: fall back to tag stripping
        return re.sub(r'<[^>]+>', ' ', html)
    return ''.join(parser.out)


def _decode(part: dict[str, Any]) -> str:
    data = (part.get('body') or {}).get('data')
    if not data:
        return ''
    try:
        raw = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ''
    charset = 'utf-8'
    for h in part.get('headers') or []:
        if str(h.get('name', '')).lower() == 'content-type':
            m = re.search(r'charset="?([\w.-]+)', str(h.get('value')), re.I)
            if m:
                charset = m.group(1)
    try:
        return raw.decode(charset, errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


def _walk(part: dict[str, Any], plain: list[str], html: list[str]) -> None:
    body = part.get('body') or {}
    if part.get('filename') or body.get('attachmentId'):
        return  # attachments are never processed
    mime = str(part.get('mimeType') or '').lower()
    for child in part.get('parts') or []:
        _walk(child, plain, html)
    if mime == 'text/plain':
        plain.append(_decode(part))
    elif mime == 'text/html':
        html.append(_decode(part))


def body_text(message: dict[str, Any]) -> str:
    plain: list[str] = []
    html: list[str] = []
    _walk(message.get('payload') or {}, plain, html)
    text = '\n'.join(t for t in plain if t.strip())
    return text if text.strip() else '\n'.join(html_to_text(h) for h in html)


# --- minimization ------------------------------------------------------------------------------------------------

_REPLY_MARKERS = re.compile(
    r'^\s*(?:'
    r'on .{3,200} wrote:|am .{3,200} schrieb .{0,80}:|le .{3,200} a écrit\s?:|el .{3,200} escribió:|'
    r'il .{3,200} ha scritto:|w dniu .{3,200} (?:pisze|napisał\(a\)|napisała|napisał):|op .{3,200} schreef .{0,80}:|'
    r'-{2,}\s*(?:original message|ursprüngliche nachricht|wiadomość oryginalna|message d\'origine)\s*-{2,}|'
    r'(?:from|von|od|de):\s.+\n\s*(?:sent|gesendet|wysłano|envoyé|date|datum|data):\s'
    r')',
    re.I | re.M,
)
_FOOTER = re.compile(
    r'unsubscribe|abmelden|wypisz się|désabonner|darse de baja|annulla iscrizione|privacy policy|'
    r'datenschutzerklärung|polityka prywatności|you are receiving this|you received this email|'
    r'this email was sent to|manage (?:your )?(?:email )?preferences',
    re.I,
)
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_PHONE = re.compile(r'(?<![\w/])(?:\+|00)\d[\d ()./-]{7,}\d(?!\w)|'
                    r'(?i:\b(?:tel|phone|telefon|tél|mobile|mob|kom)\b\.?:?\s*)[\d ()+./-]{7,}\d')
_URL = re.compile(r'https?://[^\s<>()"\']+', re.I)


def _short_url(match: re.Match) -> str:
    url = match.group(0).rstrip('.,;:!?')
    rest = url.split('://', 1)[1].split('?', 1)[0].split('#', 1)[0]
    host, _, path = rest.partition('/')
    # Keep short path segments (they often carry a job id); drop long opaque ones (tracking or auth tokens).
    segments = [s for s in path.split('/') if s and len(s) <= 24 and not re.fullmatch(r'[A-Za-z0-9_-]{20,}', s)]
    return f'[link {host.lower()}{"/" + "/".join(segments[:4]) if segments else ""}]'


@dataclass
class Minimized:
    text: str
    subject: str
    sender_name: str
    sender_domain: str


def minimize(message: dict[str, Any], *, user_name: str, user_email: str) -> Minimized:
    text = body_text(message).replace('\r\n', '\n')
    cut = _REPLY_MARKERS.search(text)
    if cut and cut.start() > 0:
        text = text[:cut.start()]
    lines: list[str] = []
    for line in text.split('\n'):
        if line.strip() in ('--', '-- ', '—'):
            break  # signature delimiter
        if line.lstrip().startswith('>'):
            continue  # quoted reply
        if _FOOTER.search(line):
            continue
        lines.append(line)
    text = '\n'.join(lines)
    text = _URL.sub(_short_url, text)
    text = _EMAIL.sub('[email]', text)
    text = _PHONE.sub('[phone]', text)
    text = _redact_self(text, user_name, user_email)
    text = re.sub(r'[ \t\u00a0]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text).strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rsplit(' ', 1)[0] + ' …'

    name, address = parseaddr(header(message, 'From'))
    subject = _redact_self(_EMAIL.sub('[email]', header(message, 'Subject')), user_name, user_email)[:300]
    return Minimized(
        text=text,
        subject=subject,
        sender_name=_redact_self(_EMAIL.sub('', name), user_name, user_email)[:120].strip(),
        sender_domain=address.rsplit('@', 1)[-1].lower()[:120] if '@' in address else '',
    )


def _redact_self(text: str, user_name: str, user_email: str) -> str:
    if user_email:
        text = re.sub(re.escape(user_email), '[you]', text, flags=re.I)
    name = (user_name or '').strip()
    if len(name) >= 3:
        text = re.sub(rf'(?<!\w){re.escape(name)}(?!\w)', '[you]', text, flags=re.I)
        for token in name.split():
            if len(token) >= 3:
                text = re.sub(rf'(?<!\w){re.escape(token)}(?!\w)', '[you]', text, flags=re.I)
    return text
