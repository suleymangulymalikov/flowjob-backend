"""Browser CV payloads, workload limits, and a read-only legacy projection."""

import json
import re
from collections import Counter
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

MAX_CONTENT_BYTES = 64 * 1024
MAX_CV_BODY_BYTES = 256 * 1024
MAX_LAYOUT_ITEMS = 600
MAX_PDF_PAGES = 16

_UNSAFE_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]')


class CvWorkloadError(ValueError):
    """A CV is too large to process within the supported workload."""


def plain_text(value: str) -> str:
    if _UNSAFE_TEXT.search(value):
        raise ValueError('Remove invalid control characters from this field.')
    return value


Text = Annotated[str, AfterValidator(plain_text)]
Label = Annotated[Text, Field(max_length=300)]
ShortText = Annotated[Text, Field(max_length=128)]
Detail = Annotated[Text, Field(max_length=4000)]
Bullet = Annotated[Text, Field(max_length=2000)]
Link = Annotated[Text, Field(max_length=2048)]
Skill = Annotated[Text, Field(max_length=200)]


def validate_workload(content: dict) -> None:
    # Compact UTF-8 is the content budget. The separate wire limit allows JSON escaping.
    encoded = json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_CONTENT_BYTES:
        raise CvWorkloadError('CV content is too large. Shorten it to 64 KiB or less.')
    experience = content.get('experience', [])
    items = len(experience) + len(content.get('education', [])) + len(content.get('projects', []))
    if items + sum(len(entry.get('bullets', [])) for entry in experience) > MAX_LAYOUT_ITEMS:
        raise CvWorkloadError('A CV can contain at most 600 experience, education, project, and bullet entries combined.')


class CvPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class CvExperience(CvPayload):
    id: ShortText = ''
    title: Label = ''
    company: Label = ''
    location: Label = ''
    start: ShortText = ''
    end: ShortText = ''
    bullets: list[Bullet] = Field(default_factory=list, max_length=40)


class CvEducation(CvPayload):
    id: ShortText = ''
    school: Label = ''
    degree: Label = ''
    start: ShortText = ''
    end: ShortText = ''
    note: Detail = ''


class CvProject(CvPayload):
    id: ShortText = ''
    name: Label = ''
    tech: Detail = ''
    description: Detail = ''


class CvContent(CvPayload):
    fullName: Label = ''
    email: Annotated[Text, Field(max_length=320)] = ''
    phone: ShortText = ''
    location: Label = ''
    links: list[Link] = Field(default_factory=list, max_length=24)
    summary: Annotated[Text, Field(max_length=8000)] = ''
    experience: list[CvExperience] = Field(default_factory=list, max_length=100)
    education: list[CvEducation] = Field(default_factory=list, max_length=100)
    projects: list[CvProject] = Field(default_factory=list, max_length=100)
    skills: list[Skill] = Field(default_factory=list, max_length=200)
    languages: Detail = ''

    @model_validator(mode='after')
    def within_workload(self) -> 'CvContent':
        validate_workload(self.model_dump())
        return self


class CvWriteInput(CvPayload):
    name: Label | None = None
    kind: Literal['original', 'tailored'] | None = None
    jobTitle: Label | None = None
    company: Label | None = None
    matchId: ShortText | None = None
    sourceCvId: ShortText | None = None
    content: CvContent | None = None


class CvCreateInput(CvWriteInput):
    tailorDraftId: ShortText | None = None


class CvPreviewInput(CvPayload):
    tailorDraftId: ShortText | None = None
    cvId: ShortText | None = None
    content: CvContent = Field(default_factory=CvContent)


def unsafe_contact_link(value: str) -> bool:
    # URL parsers may strip whitespace/control characters before interpreting a scheme.
    compact = re.sub(r'[\s\x00-\x1f\x7f]+', '', value)
    if compact.lower().startswith(('javascript:', 'vbscript:', 'data:', 'file:', 'blob:')):
        return True
    try:
        parts = urlsplit(compact)
        return parts.netloc != '' and (parts.username is not None or parts.password is not None)
    except ValueError:
        return False  # malformed old link text stays inert


def has_new_unsafe_links(links: list[str], inherited: Any) -> bool:
    return bool(Counter(link for link in links if unsafe_contact_link(link)) - Counter(_strings(inherited)))


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _legacy_items(value: Any, defaults: dict, section: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    rows = [item for item in value if isinstance(item, dict)]
    used_ids = {item['id'] for item in rows if isinstance(item.get('id'), str)}
    result = []
    for index, raw in enumerate(rows):
        item = {
            key: raw[key] if isinstance(raw.get(key), str) else default
            for key, default in defaults.items()
        }
        if 'bullets' in defaults and isinstance(bullets := raw.get('bullets'), list):
            # Keep original indices for verified tailoring drafts.
            item['bullets'] = [value if isinstance(value, str) else '' for value in bullets]
        if not item['id']:
            # Stable, non-conflicting form keys for entries predating item IDs.
            item_id = f'legacy-{section}-{index}'
            while item_id in used_ids:
                item_id += '-'
            item['id'] = item_id
            used_ids.add(item_id)
        # Optional fields need no new keys when omitted in storage.
        for key in ('location', 'note'):
            if key in defaults and key not in raw:
                item.pop(key)
        result.append(item)
    return result


def read_cv_content(content: dict) -> dict:
    """Project a legacy CV into the browser shape without changing stored content."""
    defaults = CvContent().model_dump()
    sections = {'experience': CvExperience, 'education': CvEducation, 'projects': CvProject}
    for key, default in defaults.items():
        value = content.get(key)
        if key in sections:
            defaults[key] = _legacy_items(value, sections[key]().model_dump(), key)
        elif isinstance(default, list):
            defaults[key] = _strings(value)
        elif isinstance(value, str):
            defaults[key] = value
    # Early API clients sometimes used the native structured languages array.
    if isinstance(languages := content.get('languages'), list):
        defaults['languages'] = ', '.join(
            f"{item['name']} ({item['level']})" if isinstance(item.get('level'), str) and item['level'] else item['name']
            for item in languages
            if isinstance(item, dict) and isinstance(item.get('name'), str) and item['name']
        )
    return defaults
