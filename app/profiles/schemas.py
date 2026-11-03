from typing import Annotated, Literal

from pydantic import BaseModel, Field

# CVs show month and year, e.g. "2024-09".
YearMonth = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", examples=["2024-09"])]


class Personal(BaseModel):
    headline: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None
    website: str | None = None
    summary: str | None = None


class Experience(BaseModel):
    company: str
    position: str
    location: str | None = None
    start: YearMonth
    end: YearMonth | None = None  # None means current job
    bullets: list[str] = []


class Education(BaseModel):
    institution: str
    degree: str
    field_of_study: str | None = None
    start: YearMonth
    end: YearMonth | None = None
    grade: str | None = None
    description: str | None = None


class Language(BaseModel):
    name: str
    level: Literal["A1", "A2", "B1", "B2", "C1", "C2", "Native"]


class Project(BaseModel):
    name: str
    description: str | None = None
    url: str | None = None
    technologies: list[str] = []


class Certification(BaseModel):
    name: str
    issuer: str | None = None
    date: YearMonth | None = None


class ProfileData(BaseModel):
    personal: Personal = Personal()
    experience: list[Experience] = []
    education: list[Education] = []
    skills: list[str] = []
    languages: list[Language] = []
    projects: list[Project] = []
    certifications: list[Certification] = []


class ProfileUpdate(BaseModel):
    """Each section sent replaces that section as a whole; sections not sent are kept."""

    personal: Personal | None = None
    experience: list[Experience] | None = None
    education: list[Education] | None = None
    skills: list[str] | None = None
    languages: list[Language] | None = None
    projects: list[Project] | None = None
    certifications: list[Certification] | None = None
