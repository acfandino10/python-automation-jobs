"""Rewrite a source CV for one role and render an ATS-friendly PDF.

The model may reorder bullets and rephrase a summary. Employers, schools,
projects, skills, and numbers that are absent from the source text are dropped
before anything is written to disk.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from rolefit.exceptions import CVError, LLMError
from rolefit.llm import LLMClient
from rolefit.models import (
    Applicant,
    EducationItem,
    Manifest,
    ManifestRole,
    ProjectItem,
    RoleSuggestion,
    SuggestionList,
    TailoredCV,
    unique_slug,
)

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "inc",
        "llc",
        "ltd",
        "corp",
        "company",
        "remote",
        "global",
        "group",
    }
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_SUGGEST_SYSTEM = """You are a careful career editor.
Read the CV and suggest 6 job titles this person can credibly interview
for with the experience they already have.
Return one JSON object:
{"roles": [{"title": "", "reason": "", "search_keywords": []}]}
Rules:
- Suggest 6 titles. Each title is specific, for example
  "Senior React Engineer", not "Engineer".
- Do not suggest a career change the CV does not support.
- reason is one sentence that cites real experience from the CV.
- search_keywords is 4 to 6 short search terms that already match
  skills in the CV.
Return JSON only.
"""

_TAILOR_SYSTEM = """You rewrite a résumé for one target role.
Use only facts from the source CV and the contact profile.
Return one JSON object with these keys:
target_role, full_name, headline, contact_line, summary, skills,
experience, education, projects, search_keywords.

Rules:
- Do not invent employers, titles, dates, schools, degrees, tools,
  products, or metrics.
- You may rephrase bullets and reorder bullets inside the same job
  so the most relevant facts come first.
- Keep jobs in reverse-chronological order.
- skills must already be demonstrated in the source CV.
- summary is 3 or 4 sentences in third person. Do not use I or my.
- headline is one line and must not claim a specialty the CV does not support.
- experience items use title, company, location, dates, bullets.
- education items use degree, school, dates, details.
- projects items use name, details.
  Include a project only when the source CV contains it.
- search_keywords is 5 to 8 job-search phrases grounded in the
  candidate's real skills plus the target role.
- Put truthful keywords that already appear in the source CV into the
  summary and the skills list. Do not add tools the CV never mentions.
- If a contact detail is absent from the source and the profile, omit it.
Return JSON only.
"""


def default_template_path() -> Path:
    """Locate ``ats_clean.html`` in the installed package or the source checkout.

    Raises:
        CVError: The template file is missing from both locations.
    """
    bundled = Path(__file__).resolve().parent / "templates" / "ats_clean.html"
    if bundled.is_file():
        return bundled
    checkout = Path(__file__).resolve().parents[2] / "templates" / "ats_clean.html"
    if checkout.is_file():
        return checkout
    raise CVError("ATS template templates/ats_clean.html was not found.")


def suggest_roles(llm: LLMClient, source_text: str, *, count: int = 6) -> list[RoleSuggestion]:
    """Ask the model for credible role titles grounded in the source CV.

    Args:
        llm: JSON completion client.
        source_text: Extracted master CV.
        count: Maximum titles to return, clamped to 5-7.

    Raises:
        LLMError: The model returned no usable titles.
    """
    wanted = min(7, max(5, count))
    payload = llm.complete_json(
        _SUGGEST_SYSTEM,
        f"Suggest {wanted} titles.\n\nSource CV:\n" + source_text[:14000],
        temperature=0.3,
        max_tokens=1800,
    )
    try:
        suggestions = SuggestionList.model_validate(payload).roles
    except ValidationError as exc:
        raise LLMError(f"Role suggestions were not in the expected shape: {exc}") from exc
    cleaned = [role for role in suggestions if role.title.strip()]
    if not cleaned:
        raise LLMError("The model did not return any role titles.")
    return cleaned[:wanted]


def build_tailored_cv(
    data: dict[str, Any],
    source_text: str,
    applicant: Applicant,
    role_title: str,
) -> TailoredCV:
    """Validate model output and drop claims that are not in the source CV.

    The applicant profile wins for the name and contact line so a model cannot
    replace them. Experience that names an employer absent from the source is removed.

    Raises:
        LLMError: The payload is not a résumé object.
        CVError: Every employer was ungrounded, so there is nothing safe to render.
    """
    try:
        cv = TailoredCV.model_validate(data)
    except ValidationError as exc:
        raise LLMError(f"The tailored CV was not in the expected shape: {exc}") from exc

    cv.target_role = role_title.strip() or cv.target_role
    if applicant.full_name:
        cv.full_name = applicant.full_name
    contact = applicant.contact_line()
    if contact:
        cv.contact_line = contact
    cv.summary = " ".join(cv.summary.split())[:1200]
    cv.headline = " ".join(cv.headline.split())[:180]
    cv = _ground_cv(cv, source_text, role_title)
    if not cv.full_name:
        cv.full_name = "Candidate"
    return cv


def tailor_cv(
    llm: LLMClient, source_text: str, applicant: Applicant, role_title: str
) -> TailoredCV:
    """Rewrite one role and ground the result against the source CV.

    The model is asked a second time when validation fails.
    """
    profile_lines = [
        f"Full name: {applicant.full_name}",
        f"Email: {applicant.email}",
        f"Phone: {applicant.phone}",
        f"LinkedIn: {applicant.linkedin_url}",
        f"Portfolio: {applicant.portfolio_url}",
        f"Location: {applicant.location}",
    ]
    user = (
        f"Target role: {role_title}\n\n"
        "Contact details extracted from the source CV (use these in the header when present):\n"
        + "\n".join(line for line in profile_lines if not line.endswith(": "))
        + "\n\nSource CV:\n"
        + source_text[:14000]
    )
    last_error: Exception | None = None
    prompt = user
    for _ in range(2):
        payload = llm.complete_json(_TAILOR_SYSTEM, prompt, temperature=0.2, max_tokens=4096)
        try:
            return build_tailored_cv(payload, source_text, applicant, role_title)
        except (LLMError, CVError) as exc:
            last_error = exc
            prompt = (
                user
                + f"\n\nThe previous JSON failed validation: {exc}\nFix it and return JSON only."
            )
    if isinstance(last_error, CVError):
        raise last_error
    raise LLMError(str(last_error) if last_error else "Tailoring failed.")


def render_html(cv: TailoredCV, template_path: Path | None = None) -> str:
    """Render the ATS HTML template for one tailored CV."""
    path = template_path or default_template_path()
    env = Environment(
        loader=FileSystemLoader(path.parent),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env.get_template(path.name).render(cv=cv)


def render_pdf(cv: TailoredCV, html: str, dest: Path, engine: str = "auto") -> str:
    """Write a text PDF and return the engine that produced it.

    ``auto`` prefers WeasyPrint and falls back to ReportLab. ``weasyprint`` fails
    closed when that library or its system dependencies are missing.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if engine in {"auto", "weasyprint"}:
        produced = _try_weasyprint(html, dest, required=engine == "weasyprint")
        if produced:
            return "weasyprint"
    _render_reportlab(cv, dest)
    return "reportlab"


def write_role_outputs(
    cv: TailoredCV,
    output_dir: Path,
    slug: str,
    pdf_engine: str,
) -> tuple[Path, Path, Path, str]:
    """Write the HTML, PDF, and JSON for one role.

    Returns:
        PDF path, HTML path, JSON path, and the PDF engine name.

    Raises:
        CVError: The PDF is missing or too small to be a real document.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    html = render_html(cv)
    html_path = output_dir / f"{slug}_cv.html"
    pdf_path = output_dir / f"{slug}_cv.pdf"
    metadata_path = output_dir / f"{slug}_cv.json"
    html_path.write_text(html, encoding="utf-8")
    engine_used = render_pdf(cv, html, pdf_path, engine=pdf_engine)
    metadata_path.write_text(cv.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if not pdf_path.exists() or pdf_path.stat().st_size < 400:
        raise CVError(f"PDF rendering failed for {cv.target_role}.")
    return pdf_path, html_path, metadata_path, engine_used


def load_manifest(path: Path) -> Manifest:
    """Load ``manifest.json``.

    Raises:
        CVError: The file is missing or is not a manifest.
    """
    if not path.exists():
        raise CVError(f"No manifest at {path}. Run `rolefit generate` before applying.")
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise CVError(f"Could not read the manifest at {path}: {exc}") from exc


def load_tailored_cv(role: ManifestRole) -> TailoredCV:
    """Load the JSON résumé stored next to a generated PDF.

    Raises:
        CVError: The metadata file is missing or invalid.
    """
    path = Path(role.metadata_path)
    if not path.is_file():
        raise CVError(f"Tailored CV metadata is missing for {role.slug}: {path}")
    try:
        return TailoredCV.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise CVError(f"Could not read {path}: {exc}") from exc


def merge_manifest(
    path: Path,
    source_cv: str,
    provider: str,
    model: str,
    role: ManifestRole,
    applicant: Applicant | None = None,
) -> Manifest:
    """Insert or replace one role in ``manifest.json`` and return the document."""
    existing: Manifest | None = None
    if path.exists():
        try:
            existing = Manifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            existing = None
    roles = [item for item in existing.roles if item.slug != role.slug] if existing else []
    roles.append(role)
    roles.sort(key=lambda item: item.title.lower())
    stored = applicant
    if stored is None and existing is not None:
        stored = existing.applicant
    manifest = Manifest(
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        source_cv=source_cv,
        provider=provider,
        model=model,
        applicant=stored or Applicant(),
        roles=roles,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest


def assign_slugs(titles: list[str]) -> list[tuple[str, str]]:
    """Pair each title with a unique slug, preserving order."""
    used: set[str] = set()
    return [(title, unique_slug(title, used)) for title in titles]


def display_path(path: Path) -> str:
    """Return a POSIX path relative to the working directory when possible."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def generate_tailored_cvs(
    *,
    source_path: Path,
    source_text: str,
    titles: list[str],
    applicant: Applicant,
    llm: LLMClient,
    output_dir: Path,
    pdf_engine: str,
    provider: str,
    model: str,
    location: str = "",
) -> Manifest:
    """Tailor one PDF per title and write ``manifest.json``.

    Args:
        source_path: Original CV path recorded in the manifest.
        source_text: Extracted text used as the grounding source.
        titles: Confirmed role titles.
        applicant: Header contact details.
        llm: JSON completion client.
        output_dir: Directory for PDFs and the manifest.
        pdf_engine: ``auto``, ``weasyprint``, or ``reportlab``.
        provider: Provider name stored in the manifest.
        model: Model name stored in the manifest.
        location: Preferred job location copied onto each role.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest: Manifest | None = None
    for title, slug in assign_slugs(titles):
        cv = tailor_cv(llm, source_text, applicant, title)
        pdf_path, html_path, metadata_path, _engine = write_role_outputs(
            cv, output_dir, slug, pdf_engine
        )
        role = ManifestRole(
            title=title,
            slug=slug,
            pdf_path=display_path(pdf_path),
            html_path=display_path(html_path),
            metadata_path=display_path(metadata_path),
            keywords=list(cv.search_keywords),
            location=location,
        )
        manifest = merge_manifest(
            manifest_path,
            display_path(source_path),
            provider,
            model,
            role,
            applicant,
        )
    if manifest is None:
        raise CVError("No role titles were confirmed, so no CVs were written.")
    return manifest


def _ground_cv(cv: TailoredCV, source_text: str, role_title: str) -> TailoredCV:
    source = source_text.lower()
    source_numbers = set(_NUMBER.findall(source_text))
    grounded_jobs = []
    for job in cv.experience[:12]:
        job.title = " ".join(job.title.split())
        job.company = " ".join(job.company.split())
        job.location = " ".join(job.location.split())
        job.dates = _dates_if_grounded(" ".join(job.dates.split()), source_numbers)
        if not job.company or not _mentioned(job.company, source):
            logger.info("Dropped ungrounded employer: %s", job.company or "(blank)")
            continue
        kept_bullets: list[str] = []
        for bullet in job.bullets:
            text = " ".join(bullet.split())
            if not text:
                continue
            if any(number not in source_numbers for number in _NUMBER.findall(text)):
                logger.info("Dropped bullet with a number that is not in the source CV.")
                continue
            kept_bullets.append(text[:400])
            if len(kept_bullets) == 8:
                break
        job.bullets = kept_bullets
        grounded_jobs.append(job)

    if cv.experience and not grounded_jobs:
        raise CVError(
            "The model returned employers that are not in the source CV, so no PDF was written. "
            "The tailor refuses to invent experience."
        )
    cv.experience = grounded_jobs
    cv.skills = _ground_skills(cv.skills, source)
    education: list[EducationItem] = []
    for school in cv.education:
        if not school.school or not _mentioned(school.school, source):
            continue
        school.dates = _dates_if_grounded(" ".join(school.dates.split()), source_numbers)
        school.details = _plain_lines(school.details, source_numbers)
        education.append(school)
    cv.education = education
    projects: list[ProjectItem] = []
    for project in cv.projects:
        if not project.name or not _mentioned(project.name, source):
            continue
        project.details = _plain_lines(project.details, source_numbers)
        projects.append(project)
    cv.projects = projects
    cv.search_keywords = _keywords(cv.search_keywords, cv.skills, role_title, source)
    if not cv.experience and not cv.skills and len(source_text.strip()) > 200:
        raise CVError(
            "The tailored CV had no grounded skills or experience, so no PDF was written."
        )
    return cv


def _dates_if_grounded(dates: str, source_numbers: set[str]) -> str:
    if any(number not in source_numbers for number in _NUMBER.findall(dates)):
        return ""
    return dates


def _plain_lines(lines: list[str], source_numbers: set[str]) -> list[str]:
    kept: list[str] = []
    for line in lines:
        text = " ".join(line.split())
        if not text:
            continue
        if any(number not in source_numbers for number in _NUMBER.findall(text)):
            continue
        kept.append(text[:400])
        if len(kept) == 6:
            break
    return kept


def _ground_skills(skills: list[str], source: str) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        text = " ".join(skill.split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        if key in source or _mentioned(text, source):
            kept.append(text[:80])
            seen.add(key)
        if len(kept) == 24:
            break
    return kept


def _keywords(
    model_keywords: list[str],
    skills: list[str],
    role_title: str,
    source: str,
) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = " ".join(value.split())
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        kept.append(text[:60])

    add(role_title)
    for candidate in [*model_keywords, *skills]:
        if candidate.strip().lower() == role_title.strip().lower() or _mentioned(candidate, source):
            add(candidate)
        if len(kept) >= 8:
            break
    return kept[:8]


def _mentioned(name: str, source: str) -> bool:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9+#]+", name.lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]
    if not tokens:
        return False
    return any(token in source for token in tokens)


def _try_weasyprint(html: str, dest: Path, required: bool) -> bool:
    try:
        from weasyprint import HTML
    except Exception as exc:
        if required:
            raise CVError(
                "WeasyPrint is not available. Install it and its system libraries, "
                "or set PDF_ENGINE=reportlab."
            ) from exc
        logger.info("WeasyPrint is not installed (%s). Using ReportLab.", exc)
        return False
    try:
        HTML(string=html, base_url=str(dest.parent)).write_pdf(str(dest))
        return True
    except Exception as exc:
        if required:
            raise CVError(f"WeasyPrint failed to write the PDF: {exc}") from exc
        logger.warning("WeasyPrint failed (%s). Using ReportLab.", exc)
        return False


def _render_reportlab(cv: TailoredCV, dest: Path) -> None:
    try:
        from reportlab.lib.colors import HexColor, black
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise CVError("reportlab is not installed. Run pip install -e .") from exc

    styles = getSampleStyleSheet()
    name_style = ParagraphStyle(
        "CVName",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=19,
        alignment=TA_LEFT,
        spaceAfter=2,
    )
    headline_style = ParagraphStyle(
        "CVHeadline",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=11,
        leading=14,
        spaceAfter=2,
    )
    contact_style = ParagraphStyle(
        "CVContact",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=12,
        textColor=HexColor("#222222"),
        spaceAfter=6,
    )
    section_style = ParagraphStyle(
        "CVSection",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        spaceBefore=8,
        spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "CVBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=13.5,
        spaceAfter=4,
    )
    job_style = ParagraphStyle(
        "CVJob",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=13,
        spaceBefore=6,
        spaceAfter=0,
    )
    meta_style = ParagraphStyle(
        "CVMeta",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=9.5,
        leading=12,
        spaceAfter=1,
    )
    bullet_style = ParagraphStyle(
        "CVBullet",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=13.5,
        leftIndent=12,
        bulletIndent=0,
        spaceAfter=1,
    )

    story: list[object] = []
    story.append(Paragraph(escape(cv.full_name), name_style))
    if cv.headline:
        story.append(Paragraph(escape(cv.headline), headline_style))
    if cv.contact_line:
        story.append(Paragraph(escape(cv.contact_line), contact_style))

    def add_section(title: str) -> None:
        story.append(Paragraph(escape(title), section_style))
        story.append(
            HRFlowable(width="100%", thickness=0.6, color=black, spaceBefore=0, spaceAfter=4)
        )

    if cv.summary:
        add_section("Summary")
        story.append(Paragraph(escape(cv.summary), body_style))
    if cv.skills:
        add_section("Skills")
        story.append(Paragraph(escape(" · ".join(cv.skills)), body_style))
    if cv.experience:
        add_section("Experience")
        for job in cv.experience:
            heading = job.title
            if job.company:
                heading = f"{heading} — {job.company}" if heading else job.company
            story.append(Paragraph(escape(heading), job_style))
            meta = " · ".join(part for part in (job.location, job.dates) if part)
            if meta:
                story.append(Paragraph(escape(meta), meta_style))
            for bullet in job.bullets:
                story.append(Paragraph(escape(f"- {bullet}"), bullet_style))
    if cv.projects:
        add_section("Projects")
        for project in cv.projects:
            story.append(Paragraph(escape(project.name), job_style))
            for detail in project.details:
                story.append(Paragraph(escape(f"- {detail}"), bullet_style))
    if cv.education:
        add_section("Education")
        for school in cv.education:
            heading = school.degree
            if school.school:
                heading = f"{heading} — {school.school}" if heading else school.school
            story.append(Paragraph(escape(heading or school.school), job_style))
            if school.dates:
                story.append(Paragraph(escape(school.dates), meta_style))
            for detail in school.details:
                story.append(Paragraph(escape(f"- {detail}"), bullet_style))

    if len(story) == 1:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph(escape(cv.target_role), body_style))

    document = SimpleDocTemplate(
        str(dest),
        pagesize=letter,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title=f"{cv.full_name} — {cv.target_role}",
        author=cv.full_name,
    )
    document.build(story)
