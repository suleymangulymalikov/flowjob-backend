import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from weasyprint import HTML

from app.profiles.schemas import ProfileData

env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(),
)

# Tried in order until the CV fits on one page: (font size in pt, page margin in inches).
FIT_STEPS = [(11, 0.75), (10.5, 0.6), (10, 0.5), (9.5, 0.5), (9, 0.4)]

# Structured native dates are strictly "YYYY-MM"; the browser CV builder also allows
# free-form text (e.g. "Summer 2019"), which must be shown verbatim rather than dropped.
_YEAR_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# Contact links: LinkedIn / GitHub URLs are shown as short clickable labels instead of raw URLs.
_LABELLED_HOSTS = {"linkedin.com": "LinkedIn", "github.com": "GitHub"}
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")


def _safe_url(value: str) -> tuple[str, str] | None:
    """(host, https URL) for a plain web link, adding a missing https://; None for anything unsafe."""
    text = (value or "").strip()
    if not text or re.search(r"\s", text):
        return None
    if _SCHEME.match(text):
        if not re.match(r"^https?://", text, re.I):
            return None  # javascript:, data:, mailto:, ftp: ... are never linked
        text = re.sub(r"^http://", "https://", text, flags=re.I)
    else:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if "." not in host or parts.username or parts.password:
        return None
    return host.removeprefix("www."), text


def contact_item(value: str) -> str | Markup:
    """A LinkedIn/GitHub link becomes a labelled link (inheriting the template's plain styling).

    Any other value is returned unchanged so unrelated legacy entries are never altered or dropped.
    """
    safe = _safe_url(value)
    if safe:
        host, url = safe
        for domain, label in _LABELLED_HOSTS.items():
            if host == domain or host.endswith("." + domain):
                return Markup('<a href="{}" style="color:inherit;text-decoration:none">{}</a>').format(url, label)
    return value


def format_month(value: str) -> str:
    return datetime.strptime(value, "%Y-%m").strftime("%b %Y")


def format_dates(start: str, end: str | None) -> str:
    return f"{format_month(start)} – {format_month(end) if end else 'Present'}"


def _loose_month(value: Any) -> str | None:
    """Format a "YYYY-MM" string as "Mon YYYY"; pass any other non-empty text through as-is."""
    text = str(value or "").strip()
    if not text:
        return None
    return format_month(text) if _YEAR_MONTH.fullmatch(text) else text


def format_dates_loose(start: Any, end: Any) -> str:
    """Date range that tolerates free-form values and missing parts (never raises)."""
    s, e = _loose_month(start), _loose_month(end)
    if not s and not e:
        return ""
    if s and not e:
        return f"{s} – Present"
    if e and not s:
        return e
    return f"{s} – {e}"


def build_context(name: str, email: str, profile: ProfileData) -> dict:
    p = profile.personal
    skills_lines = []
    if profile.skills:
        skills_lines.append(("Technical", ", ".join(profile.skills)))
    if profile.languages:
        skills_lines.append(("Languages", ", ".join(f"{lang.name} ({lang.level})" for lang in profile.languages)))
    if profile.certifications:
        certifications = []
        for c in profile.certifications:
            details = [d for d in (c.issuer, format_month(c.date) if c.date else None) if d]
            certifications.append(c.name + (f" ({', '.join(details)})" if details else ""))
        skills_lines.append(("Certifications", "; ".join(certifications)))

    return {
        "name": name,
        "contact": [
            v if k in ("location", "phone", "email") else contact_item(v)
            for k, v in (
                ("location", p.location), ("phone", p.phone), ("email", email),
                ("linkedin", p.linkedin), ("github", p.github), ("website", p.website),
            )
            if v
        ],
        "summary": p.summary,
        "education": [{**e.model_dump(), "dates": format_dates(e.start, e.end)} for e in profile.education],
        "experience": [{**e.model_dump(), "dates": format_dates(e.start, e.end)} for e in profile.experience],
        "projects": [project.model_dump() for project in profile.projects],
        "skills_lines": skills_lines,
    }


def build_cv_context(name: str, email: str, content: dict[str, Any]) -> dict:
    """Build the Harvard template context from a saved CV's browser content.

    Maps the frontend CV shape (title/school/tech/free-form dates, free-form languages)
    onto the same template the native profile uses, without discarding any entered data.
    """
    c = content or {}
    contact = [v for v in (c.get("location"), c.get("phone"), email or c.get("email")) if v]
    contact += [contact_item(str(link)) for link in (c.get("links") or []) if link]

    experience = [
        {
            "company": e.get("company") or "",
            "position": e.get("title") or "",
            "location": e.get("location") or "",
            "bullets": [b for b in (e.get("bullets") or []) if b],
            "dates": format_dates_loose(e.get("start"), e.get("end")),
        }
        for e in (c.get("experience") or [])
    ]
    education = [
        {
            "institution": e.get("school") or "",
            "degree": e.get("degree") or "",
            "field_of_study": None,
            "grade": None,
            "description": e.get("note") or None,
            "dates": format_dates_loose(e.get("start"), e.get("end")),
        }
        for e in (c.get("education") or [])
    ]
    projects = [
        {
            "name": p.get("name") or "",
            "url": None,
            "technologies": [t.strip() for t in str(p.get("tech") or "").split(",") if t.strip()],
            "description": p.get("description") or None,
        }
        for p in (c.get("projects") or [])
    ]

    skills_lines = []
    if c.get("skills"):
        skills_lines.append(("Technical", ", ".join(c["skills"])))
    if c.get("languages"):  # free-form text, preserved verbatim
        skills_lines.append(("Languages", str(c["languages"])))

    return {
        "name": name or c.get("fullName") or "",
        "contact": contact,
        "summary": c.get("summary") or "",
        "education": education,
        "experience": experience,
        "projects": projects,
        "skills_lines": skills_lines,
    }


def _render_context_to_pdf(context: dict) -> bytes:
    """Render the Harvard template, shrinking font and margins until it fits on one page.

    If it still doesn't fit at the smallest step, the multi-page result is returned.
    """
    template = env.get_template("harvard.html")
    for font_size, margin in FIT_STEPS:
        html = template.render(**context, font_size=font_size, margin=margin)
        document = HTML(string=html).render()
        if len(document.pages) == 1:
            break
    return document.write_pdf()


def render_cv_pdf(name: str, email: str, profile: ProfileData) -> bytes:
    """Render a Harvard-style CV from the native profile model."""
    return _render_context_to_pdf(build_context(name, email, profile))


def render_saved_cv_pdf(name: str, email: str, content: dict[str, Any]) -> bytes:
    """Render a Harvard-style CV from a saved CV's browser content."""
    return _render_context_to_pdf(build_cv_context(name, email, content))
