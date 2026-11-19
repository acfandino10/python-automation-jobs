"""Shared data models for tailored CVs, manifests, and applicants."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into a first name and the remaining last name."""
    parts = [part for part in (full_name or "").split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _resolved_names(full_name: str, first_name: str, last_name: str) -> tuple[str, str, str]:
    """Keep an explicit first or last name. Fill whichever side is missing from the full name."""
    full_name = " ".join((full_name or "").split())
    first_name = " ".join((first_name or "").split())
    last_name = " ".join((last_name or "").split())
    if full_name and not first_name and not last_name:
        first_name, last_name = split_name(full_name)
    elif not full_name and (first_name or last_name):
        full_name = " ".join(part for part in (first_name, last_name) if part)
    return first_name, last_name, full_name


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


_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE_PREFIX = re.compile(r"^\+(\d{1,3})(?:[\s().-]+)?(.*)$")


def split_phone(phone: str) -> tuple[str, str]:
    """Split ``+57 319 482 6159`` into ``("+57", "319 482 6159")``."""
    cleaned = " ".join((phone or "").split())
    match = _PHONE_PREFIX.match(cleaned)
    if match is None:
        return "", cleaned
    number = " ".join(match.group(2).split())
    return f"+{match.group(1)}", number


def split_place(location: str) -> tuple[str, str]:
    """Split ``Bogotá, Colombia`` into ``("Bogotá", "Colombia")``."""
    parts = [part.strip() for part in (location or "").split(",") if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def parse_contact_line(line: str) -> dict[str, str]:
    """Split a pipe-separated header into the contact fields forms ask for."""
    found = {
        "email": "",
        "phone_prefix": "",
        "phone": "",
        "linkedin": "",
        "portfolio": "",
        "city": "",
        "country": "",
    }
    place = ""
    for part in re.split(r"\s*\|\s*", line or ""):
        piece = part.strip().strip("·•")
        if not piece:
            continue
        email_match = _EMAIL.search(piece)
        if email_match and not found["email"]:
            found["email"] = email_match.group(0)
            continue
        if "linkedin.com" in piece.lower() and not found["linkedin"]:
            found["linkedin"] = _ensure_https(piece)
            continue
        lowered = piece.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            if not found["portfolio"]:
                found["portfolio"] = piece.rstrip(".,;")
            continue
        prefix, number = split_phone(piece)
        if (prefix or sum(char.isdigit() for char in piece) >= 7) and not found["phone"]:
            found["phone_prefix"] = prefix
            found["phone"] = number
            continue
        if not place:
            place = piece.strip(" ,")
    if place:
        found["city"], found["country"] = split_place(place)
    return found


def _ensure_https(value: str) -> str:
    cleaned = value.strip().strip("·•,;")
    if cleaned.lower().startswith("http://"):
        return "https://" + cleaned[7:]
    if cleaned.lower().startswith("https://"):
        return cleaned
    return "https://" + cleaned


class Applicant(BaseModel):
    """Contact facts typed into application forms. Empty fields are left blank."""

    model_config = ConfigDict(extra="ignore")

    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    location: str = ""
    work_authorization: str = ""
    salary_expectation: str = ""
    school: str = ""

    @model_validator(mode="after")
    def _fill_name_parts(self) -> Applicant:
        self.first_name, self.last_name, self.full_name = _resolved_names(
            self.full_name, self.first_name, self.last_name
        )
        return self

    @property
    def phone_prefix(self) -> str:
        return split_phone(self.phone)[0]

    @property
    def phone_number(self) -> str:
        return split_phone(self.phone)[1]

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
    first_name: str = ""
    last_name: str = ""
    headline: str = ""
    email: str = ""
    phone_prefix: str = ""
    phone: str = ""
    linkedin: str = ""
    portfolio: str = ""
    city: str = ""
    country: str = ""
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    search_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _expand_contact_line(cls, data: Any) -> Any:
        """Older extracts stored one contact_line. Split it into form fields."""
        if not isinstance(data, dict):
            return data
        line = str(data.get("contact_line") or "").strip()
        if not line:
            return data
        merged = dict(data)
        for key, value in parse_contact_line(line).items():
            if value and not str(merged.get(key) or "").strip():
                merged[key] = value
        return merged

    @model_validator(mode="after")
    def _fill_name_parts(self) -> TailoredCV:
        self.first_name, self.last_name, self.full_name = _resolved_names(
            self.full_name, self.first_name, self.last_name
        )
        return self

    @property
    def contact_line(self) -> str:
        """Single header line used on the rendered résumé."""
        number = " ".join(part for part in (self.phone_prefix, self.phone) if part)
        place = ", ".join(part for part in (self.city, self.country) if part)
        parts = [self.email, number, self.linkedin, self.portfolio, place]
        return " | ".join(part.strip() for part in parts if part and part.strip())

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
