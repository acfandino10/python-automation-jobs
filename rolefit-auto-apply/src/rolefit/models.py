"""Domain schemas for CVs, manifests, job listings, and application rows."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ApplicationStatus = Literal[
    "submitted",
    "pending_review",
    "dry_run",
    "below_threshold",
    "skipped_manual",
    "error",
]


def slugify(value: str) -> str:
    """Turn a role title into a filesystem-safe slug.

    Args:
        value: Human-readable role title.

    Returns:
        A lowercase slug. Empty input becomes ``role``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "role"


def unique_slug(title: str, used: set[str]) -> str:
    """Return a slug that is not already present in ``used``.

    The set is updated with the chosen slug.

    Args:
        title: Role title to slugify.
        used: Slugs already assigned in this batch.

    Returns:
        A unique slug such as ``data-engineer`` or ``data-engineer-2``.
    """
    base = slugify(title)
    slug = base
    number = 2
    while slug in used:
        slug = f"{base}-{number}"
        number += 1
    used.add(slug)
    return slug


class Applicant(BaseModel):
    """Contact facts typed into application forms.

    Empty fields are left blank. The tailor uses the same values in the CV header.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

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
        """Return the first whitespace-separated token of the full name."""
        parts = self.full_name.split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        """Return every token after the first name."""
        parts = self.full_name.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""

    def contact_line(self) -> str:
        """Join the non-empty contact fields for the CV header."""
        parts = [self.email, self.phone, self.linkedin_url, self.portfolio_url, self.location]
        return " | ".join(part.strip() for part in parts if part and part.strip())


class ExperienceItem(BaseModel):
    """One job entry on a résumé."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str = ""
    company: str = ""
    location: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    """One education entry on a résumé."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    degree: str = ""
    school: str = ""
    dates: str = ""
    details: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    """One project entry on a résumé."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = ""
    details: list[str] = Field(default_factory=list)


class CVData(BaseModel):
    """Structured parse of a master CV.

    ``raw_text`` is the extracted source. Tailoring is grounded on that text,
    not on fields a parser or model might have inferred.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    full_name: str = ""
    headline: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    raw_text: str = ""


class TailoredCV(BaseModel):
    """Structured résumé content for one target role.

    Renderers use this object. They do not accept free-form HTML from a model.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

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
        """Return a plain-text résumé used for scoring and screening answers."""
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
                heading = " — ".join(
                    part for part in (job.title, job.company, job.location, job.dates) if part
                )
                lines.append(heading)
                lines.extend(f"- {bullet}" for bullet in job.bullets)
        if self.education:
            lines.extend(["", "Education"])
            for school in self.education:
                heading = " — ".join(
                    part for part in (school.degree, school.school, school.dates) if part
                )
                lines.append(heading)
                lines.extend(f"- {detail}" for detail in school.details)
        if self.projects:
            lines.extend(["", "Projects"])
            for project in self.projects:
                lines.append(project.name)
                lines.extend(f"- {detail}" for detail in project.details)
        return "\n".join(line for line in lines if line is not None).strip()


class RoleSuggestion(BaseModel):
    """One role title the model believes the CV can support."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

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
    """JSON envelope for role suggestions."""

    model_config = ConfigDict(extra="ignore")

    roles: list[RoleSuggestion] = Field(default_factory=list)


class ManifestRole(BaseModel):
    """One generated role profile recorded in ``manifest.json``."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str
    slug: str
    pdf_path: str
    html_path: str = ""
    metadata_path: str = ""
    keywords: list[str] = Field(default_factory=list)
    location: str = ""


class Manifest(BaseModel):
    """Index of tailored PDFs produced by ``rolefit generate``."""

    model_config = ConfigDict(extra="ignore")

    generated_at: str
    source_cv: str
    provider: str
    model: str
    applicant: Applicant = Field(default_factory=Applicant)
    roles: list[ManifestRole] = Field(default_factory=list)

    def role_by_query(self, query: str) -> ManifestRole | None:
        """Find a role by slug or exact title.

        Args:
            query: Slug or title, compared case-insensitively.

        Returns:
            The matching role, or ``None``.
        """
        needle = query.strip().lower()
        for role in self.roles:
            if needle in {role.slug.lower(), role.title.lower()}:
                return role
        return None


class JobListing(BaseModel):
    """A single opening returned by a job-board adapter."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str
    company: str
    url: str
    location: str = ""
    description: str = ""
    board: str = ""
    external_id: str = ""

    @field_validator("title", "company", "url")
    @classmethod
    def _required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Job title, company, and URL are required.")
        return cleaned

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("Job URL must start with http:// or https://.")
        return value


class JobFit(BaseModel):
    """How well a tailored CV matches one job description."""

    model_config = ConfigDict(extra="ignore")

    score: int = Field(ge=0, le=100)
    summary: str = ""
    dealbreakers: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, value: Any) -> int:
        return max(0, min(100, round(float(value))))


class ScreeningAnswer(BaseModel):
    """A short answer for one application question."""

    model_config = ConfigDict(extra="ignore")

    answer: str = ""
    needs_review: bool = False

    @field_validator("answer")
    @classmethod
    def _answer(cls, value: str) -> str:
        return " ".join(value.replace("\n", " ").split())


class ApplicationRecord(BaseModel):
    """One row in the application audit log."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    job_title: str
    company: str
    url: str
    cv_used: str
    match_score: float | None = None
    timestamp: str
    status: ApplicationStatus
    notes: str = ""

    def csv_row(self) -> dict[str, str]:
        """Return the spreadsheet representation of this attempt."""
        score = "" if self.match_score is None else f"{self.match_score:.0f}"
        return {
            "Job Title": self.job_title,
            "Company": self.company,
            "URL": self.url,
            "CV Used": self.cv_used,
            "Match Score": score,
            "Timestamp": self.timestamp,
            "Status": self.status,
            "Notes": self.notes,
        }
