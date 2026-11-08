"""Request schemas for the applications API. Field names are camelCase to match the UI."""
from datetime import UTC, date, datetime

from pydantic import BaseModel, Field, field_validator

from app.applications.models import ApplicationStatus, WorkArrangement


def _clean_url(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if ' ' in v or '.' not in v or len(v) > 2048:
        raise ValueError('Enter a valid URL.')
    return v


def _not_future(v: date | None) -> date | None:
    if v and v > datetime.now(UTC).date():
        raise ValueError('The date cannot be in the future.')
    return v


class ApplicationCreate(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    jobTitle: str = Field(min_length=1, max_length=200)
    status: ApplicationStatus = ApplicationStatus.saved
    jobUrl: str | None = None
    location: str | None = Field(default=None, max_length=200)
    workArrangement: WorkArrangement | None = None
    employmentType: str | None = Field(default=None, max_length=50)
    dateApplied: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    cvId: str | None = None
    followUpDate: date | None = None

    _v_url = field_validator('jobUrl')(_clean_url)
    _v_date = field_validator('dateApplied')(_not_future)


class ApplicationUpdate(BaseModel):
    """All fields optional; only those sent are changed (PATCH semantics)."""

    company: str | None = Field(default=None, min_length=1, max_length=200)
    jobTitle: str | None = Field(default=None, min_length=1, max_length=200)
    status: ApplicationStatus | None = None
    jobUrl: str | None = None
    location: str | None = Field(default=None, max_length=200)
    workArrangement: WorkArrangement | None = None
    employmentType: str | None = Field(default=None, max_length=50)
    dateApplied: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    cvId: str | None = None
    followUpDate: date | None = None

    _v_url = field_validator('jobUrl')(_clean_url)
    _v_date = field_validator('dateApplied')(_not_future)
