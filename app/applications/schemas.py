"""Request schemas for the applications API. Field names are camelCase to match the UI."""
from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.applications.models import ApplicationStatus, WorkArrangement

ApplicationLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


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
    company: ApplicationLabel
    jobTitle: ApplicationLabel
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
    """Only supplied fields change. Null clears an optional value."""

    company: ApplicationLabel | None = None
    jobTitle: ApplicationLabel | None = None
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

    @field_validator('company', 'jobTitle', 'status', mode='before')
    @classmethod
    def required_when_supplied(cls, value):
        # Pydantic does not validate the omitted defaults, only supplied values.
        if value is None:
            raise ValueError('This field cannot be null.')
        return value
