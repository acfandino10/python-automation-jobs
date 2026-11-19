"""Shared data models for tailored CVs, manifests, and applicants."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def slugify(value: str) -> str:
    """Turn a role title into a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "role"


def unique_slug(title: str, used: set[str]) -> str:
    base = slugify(title)
    slug = base
    number = 2
    while slug in used:
        slug = f"{base}-{number}"
        number += 1
    used.add(slug)
    return slug


class Applicant(BaseModel):
    """Contact facts typed into application forms. Empty fields are left blank."""

    model_config = ConfigDict(extra="ignore")

    full_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    location: str = ""
    work_authorization: str = ""
    salary_expectation: str = ""

    @property
    def first_name(self) -> str:
        parts = self.full_name.split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        parts = self.full_name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    def contact_line(self) -> str:
        parts = [self.email, self.phone, self.linkedin_url, self.portfolio_url, self.location]
        return " | ".join(part.strip() for part in parts if part and part.strip())


class ExperienceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    company: str = ""
    location: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    degree: str = ""
    school: str = ""
    dates: str = ""
    details: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    details: list[str] = Field(default_factory=list)


class TailoredCV(BaseModel):
    """Structured résumé content. Renderers must use this object, not free-form HTML from the model."""

    model_config = ConfigDict(extra="ignore")

    target_role: str
    full_name: str = ""
    headline: str = ""
    contact_line: str = ""
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    search_keywords: list[str] = Field(default_factory=list)

    def as_text(self) -> str:
        lines = [
            self.full_name,
            self.headline,
            self.contact_line,
            "",
            f"Target role: {self.target_role}",
            "",
            "Summary",
            self.summary,
            "",
            "Skills",
            ", ".join(self.skills),
        ]
        if self.experience:
            lines.extend(["", "Experience"])
            for job in self.experience:
                lines.append(f"{job.title} — {job.company} — {job.location} — {job.dates}".strip(" —"))
                lines.extend(f"- {bullet}" for bullet in job.bullets)
        if self.education:
            lines.extend(["", "Education"])
            for school in self.education:
                lines.append(f"{school.degree} — {school.school} — {school.dates}".strip(" —"))
                lines.extend(f"- {detail}" for detail in school.details)
        if self.projects:
            lines.extend(["", "Projects"])
            for project in self.projects:
                lines.append(project.name)
                lines.extend(f"- {detail}" for detail in project.details)
        return "\n".join(line for line in lines if line is not None).strip()


class RoleSuggestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    reason: str = ""
    search_keywords: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Role title is empty.")
        return cleaned


class SuggestionList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    roles: list[RoleSuggestion] = Field(default_factory=list)


class ManifestRole(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    slug: str
    pdf_path: str
    html_path: str
    metadata_path: str
    keywords: list[str] = Field(default_factory=list)
    location: str = ""


class Manifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str
    source_cv: str
    provider: str
    model: str
    person_slug: str = ""
    applicant: Applicant = Field(default_factory=Applicant)
    roles: list[ManifestRole] = Field(default_factory=list)

    def role_by_query(self, query: str) -> ManifestRole | None:
        needle = query.strip().lower()
        for role in self.roles:
            if needle in {role.slug.lower(), role.title.lower()}:
                return role
        return None


class JobFit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: int = Field(ge=0, le=100)
    summary: str = ""
    dealbreakers: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, value: Any) -> int:
        return max(0, min(100, int(round(float(value)))))


class ScreeningAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answer: str = ""
    needs_review: bool = False

    @field_validator("answer")
    @classmethod
    def _answer(cls, value: str) -> str:
        return " ".join(value.replace("\n", " ").split())
