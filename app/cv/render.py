from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from app.profiles.schemas import ProfileData

env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(),
)

# Tried in order until the CV fits on one page: (font size in pt, page margin in inches).
FIT_STEPS = [(11, 0.75), (10.5, 0.6), (10, 0.5), (9.5, 0.5), (9, 0.4)]


def format_month(value: str) -> str:
    return datetime.strptime(value, "%Y-%m").strftime("%b %Y")


def format_dates(start: str, end: str | None) -> str:
    return f"{format_month(start)} – {format_month(end) if end else 'Present'}"


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
        "contact": [v for v in (p.location, p.phone, email, p.linkedin, p.github, p.website) if v],
        "summary": p.summary,
        "education": [{**e.model_dump(), "dates": format_dates(e.start, e.end)} for e in profile.education],
        "experience": [{**e.model_dump(), "dates": format_dates(e.start, e.end)} for e in profile.experience],
        "projects": [project.model_dump() for project in profile.projects],
        "skills_lines": skills_lines,
    }


def render_cv_pdf(name: str, email: str, profile: ProfileData) -> bytes:
    """Render a Harvard-style CV, shrinking font and margins until it fits on one page.

    If it still doesn't fit at the smallest step, the multi-page result is returned.
    """
    template = env.get_template("harvard.html")
    context = build_context(name, email, profile)
    for font_size, margin in FIT_STEPS:
        html = template.render(**context, font_size=font_size, margin=margin)
        document = HTML(string=html).render()
        if len(document.pages) == 1:
            break
    return document.write_pdf()
