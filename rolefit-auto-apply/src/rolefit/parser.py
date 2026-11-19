"""Extract and lightly structure text from a PDF, DOCX, or plain-text CV.

New file types register a :class:`DocumentExtractor` on an :class:`ExtractorRegistry`.
The registry is the extension point. Callers keep using :func:`parse_cv`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from rolefit.exceptions import CVError
from rolefit.models import Applicant, CVData, EducationItem, ExperienceItem, ProjectItem

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+?\d[\d().\-\s]{7,}\d)")
_LINKEDIN = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[^\s|<>]+", re.IGNORECASE)
_PORTFOLIO = re.compile(
    r"https?://(?!(?:www\.)?linkedin\.com\b)[^\s|<>]+",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")

_SECTION_NAMES = {
    "summary": {"summary", "professional summary", "profile", "about"},
    "skills": {"skills", "technical skills", "core skills", "technologies"},
    "experience": {"experience", "work experience", "employment", "professional experience"},
    "education": {"education", "academic background"},
    "projects": {"projects", "selected projects", "personal projects"},
}


class DocumentExtractor(Protocol):
    """Read one résumé file type into plain text."""

    suffixes: tuple[str, ...]

    def extract(self, path: Path) -> str:
        """Return the text content of ``path``."""


class ExtractorRegistry:
    """Map a file suffix to an extractor.

    Contributors add a format by implementing :class:`DocumentExtractor` and
    calling :meth:`register`. The first registered extractor that lists the
    suffix wins.
    """

    def __init__(self) -> None:
        self._extractors: list[DocumentExtractor] = []

    def register(self, extractor: DocumentExtractor) -> None:
        """Add an extractor. Later registrations do not replace earlier ones."""
        self._extractors.append(extractor)

    def extract(self, path: Path) -> str:
        """Extract text from a résumé file.

        Args:
            path: PDF, DOCX, TXT, or another registered type.

        Returns:
            Cleaned text of at least 40 characters.

        Raises:
            CVError: The file is missing, the type is unknown, or the text layer is empty.
        """
        if not path.exists():
            raise CVError(f"CV file not found: {path}")
        if not path.is_file():
            raise CVError(f"CV path is not a file: {path}")
        suffix = path.suffix.lower()
        for extractor in self._extractors:
            if suffix in extractor.suffixes:
                text = extractor.extract(path).replace("\x00", "").strip()
                if len(text) < 40:
                    raise CVError(
                        f"Almost no text was extracted from {path.name}. "
                        "Export a text-based PDF or DOCX. Scanned image PDFs are not supported."
                    )
                return text
        supported = sorted({suffix for item in self._extractors for suffix in item.suffixes})
        raise CVError(f"Unsupported CV type '{suffix}'. Supported types: {', '.join(supported)}.")


class PdfExtractor:
    """Extract text from a PDF with pypdf.

    pdfplumber is used as well when it is installed, and the longer extraction wins.
    """

    suffixes: tuple[str, ...] = (".pdf",)

    def extract(self, path: Path) -> str:
        """Return text from every page."""
        pypdf_text = self._pypdf(path)
        plumber_text = self._pdfplumber(path)
        if len(pypdf_text.strip()) > len(plumber_text.strip()):
            return pypdf_text
        return plumber_text or pypdf_text

    def _pdfplumber(self, path: Path) -> str:
        try:
            import pdfplumber
        except ImportError:
            return ""
        try:
            pages: list[str] = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    pages.append(page.extract_text() or "")
            return "\n\n".join(pages)
        except Exception:
            return ""

    def _pypdf(self, path: Path) -> str:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except ImportError as exc:
            raise CVError("pypdf is not installed. Run pip install -e .") from exc
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception as exc:
                    raise CVError(f"{path.name} is encrypted. Export an unlocked PDF.") from exc
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except CVError:
            raise
        except PdfReadError as exc:
            raise CVError(f"Could not read {path.name}: {exc}") from exc
        except Exception as exc:
            raise CVError(f"Could not read {path.name}: {exc}") from exc


class DocxExtractor:
    """Extract paragraphs and table cells from a DOCX résumé."""

    suffixes: tuple[str, ...] = (".docx",)

    def extract(self, path: Path) -> str:
        """Return paragraph and table text in document order."""
        try:
            from docx import Document
        except ImportError as exc:
            raise CVError("python-docx is not installed. Run pip install -e .") from exc
        try:
            document = Document(str(path))
        except Exception as exc:
            raise CVError(f"Could not read {path.name}: {exc}") from exc
        parts: list[str] = [
            paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()
        ]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(dict.fromkeys(cells)))
        return "\n".join(parts)


class TextExtractor:
    """Read a UTF-8 ``.txt`` résumé."""

    suffixes: tuple[str, ...] = (".txt",)

    def extract(self, path: Path) -> str:
        """Return the file contents."""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CVError(f"Could not read {path.name}: {exc}") from exc


def default_registry() -> ExtractorRegistry:
    """Return a registry with the built-in PDF, DOCX, and text extractors."""
    registry = ExtractorRegistry()
    registry.register(PdfExtractor())
    registry.register(DocxExtractor())
    registry.register(TextExtractor())
    return registry


def extract_text(path: Path, registry: ExtractorRegistry | None = None) -> str:
    """Extract plain text from a résumé file.

    Args:
        path: Source CV.
        registry: Optional registry. The default registry is used when omitted.
    """
    return (registry or default_registry()).extract(path)


def parse_cv(path: Path, registry: ExtractorRegistry | None = None) -> CVData:
    """Parse a résumé file into a :class:`CVData` document.

    Section detection is heuristic. ``raw_text`` always holds the full extraction
    so later tailoring can reject claims that the section parser missed.

    Args:
        path: PDF, DOCX, or TXT résumé.
        registry: Optional extractor registry.
    """
    text = extract_text(path, registry)
    data = structure_text(text)
    return data


def structure_text(text: str) -> CVData:
    """Build a :class:`CVData` document from résumé text.

    Args:
        text: Plain text, typically the output of :func:`extract_text`.
    """
    sections = _split_sections(text)
    email_match = _EMAIL.search(text)
    phone_match = _PHONE.search(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    email = email_match.group(0) if email_match else ""
    return CVData(
        full_name=_guess_name(lines, email),
        email=email,
        phone=phone_match.group(0).strip() if phone_match else "",
        location=_find_location(lines),
        summary=" ".join(sections.get("summary", "").split()),
        skills=_split_skills(sections.get("skills", "")),
        experience=_parse_experience(sections.get("experience", "")),
        education=_parse_education(sections.get("education", "")),
        projects=_parse_projects(sections.get("projects", "")),
        raw_text=text.strip(),
    )


def _split_sections(text: str) -> dict[str, str]:
    current = "preamble"
    buckets: dict[str, list[str]] = {current: []}
    for line in text.splitlines():
        key = _heading_key(line)
        if key:
            current = key
            buckets.setdefault(current, [])
            continue
        buckets.setdefault(current, []).append(line)
    return {name: "\n".join(lines).strip() for name, lines in buckets.items()}


def _heading_key(line: str) -> str | None:
    token = line.strip().strip(":").lower()
    if not token or len(token) > 40:
        return None
    for key, names in _SECTION_NAMES.items():
        if token in names:
            return key
    return None


def applicant_from_text(text: str) -> Applicant:
    """Build the contact record from résumé text.

    Name, email, phone, links, location, work authorization, and salary come
    from the CV. Environment variables are not consulted.
    """
    data = structure_text(text)
    return Applicant(
        full_name=data.full_name,
        email=data.email,
        phone=data.phone,
        linkedin_url=_clean_url(_first_match(_LINKEDIN, text)),
        portfolio_url=_clean_url(_first_match(_PORTFOLIO, text)),
        location=data.location,
        work_authorization=_labeled_line(
            text,
            ("authorized to work", "work authorization", "require sponsorship", "requires sponsorship"),
        ),
        salary_expectation=_labeled_line(text, ("salary expectation", "expected salary")),
    )


def _guess_name(lines: list[str], email: str) -> str:
    for line in lines[:6]:
        if email and email in line:
            continue
        if _heading_key(line):
            continue
        if _PHONE.search(line):
            continue
        words = line.split()
        if 1 < len(words) <= 5 and all(word[:1].isalpha() for word in words):
            return line
    return ""


def _split_skills(section: str) -> list[str]:
    if not section:
        return []
    chunks = re.split(r"[,•|\n;/]", section)
    skills: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        skill = " ".join(chunk.split()).strip(" -*")
        key = skill.lower()
        if not skill or key in seen or len(skill) > 60:
            continue
        seen.add(key)
        skills.append(skill)
    return skills


def _parse_experience(section: str) -> list[ExperienceItem]:
    items: list[ExperienceItem] = []
    for block in _blocks(section):
        title, company, dates = _split_heading(block[0])
        bullets = [_bullet(line) for line in block[1:] if _bullet(line)]
        if not (title or company or bullets):
            continue
        items.append(ExperienceItem(title=title, company=company, dates=dates, bullets=bullets))
    return items


def _parse_education(section: str) -> list[EducationItem]:
    items: list[EducationItem] = []
    for block in _blocks(section):
        degree, school, dates = _split_heading(block[0])
        details = [_bullet(line) for line in block[1:] if _bullet(line)]
        if not (degree or school):
            continue
        items.append(EducationItem(degree=degree, school=school, dates=dates, details=details))
    return items


def _parse_projects(section: str) -> list[ProjectItem]:
    items: list[ProjectItem] = []
    for block in _blocks(section):
        name = block[0].lstrip("-• ").strip()
        details = [_bullet(line) for line in block[1:] if _bullet(line)]
        if name:
            items.append(ProjectItem(name=name, details=details))
    return items


def _blocks(section: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in section.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line.strip())
    if current:
        blocks.append(current)
    return blocks


def _split_heading(line: str) -> tuple[str, str, str]:
    cleaned = line.lstrip("-• ").strip()
    parts = [
        part.strip()
        for part in re.split(r"\s+(?:\u2014|\u2013|\|)\s+|\s+-\s+", cleaned)
        if part.strip()
    ]
    dates = ""
    kept: list[str] = []
    for part in parts:
        if _YEAR.search(part) and not dates:
            dates = part
            continue
        kept.append(part)
    if len(parts) == 1 and " at " in cleaned.lower():
        left, right = re.split(r"\s+at\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)
        return left.strip(), right.strip(), dates
    title = kept[0] if kept else ""
    company = kept[1] if len(kept) > 1 else ""
    return title, company, dates


def _find_location(lines: list[str]) -> str:
    for line in lines[:8]:
        if "|" not in line:
            continue
        for part in re.split(r"\s*\|\s*", line):
            if _EMAIL.search(part) or _LINKEDIN.search(part) or _PHONE.search(part):
                continue
            cleaned = part.strip(" ·•-|,")
            if cleaned and len(cleaned) <= 80 and "@" not in cleaned:
                return cleaned
    return ""


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(0) if match else ""


def _clean_url(value: str) -> str:
    cleaned = value.strip().strip("·•,;")
    if not cleaned:
        return ""
    cleaned = cleaned.split("?")[0].rstrip("/).,;")
    if cleaned.lower().startswith("http://"):
        return "https://" + cleaned[7:]
    if cleaned.lower().startswith("https://"):
        return cleaned
    return "https://" + cleaned


def _labeled_line(text: str, needles: tuple[str, ...]) -> str:
    for line in text.splitlines():
        cleaned = " ".join(line.split()).strip(" -•*")
        lowered = cleaned.lower()
        if cleaned and len(cleaned) <= 200 and any(needle in lowered for needle in needles):
            return cleaned
    return ""


def _bullet(line: str) -> str:
    text = line.strip()
    if text.startswith(("-", "•", "*")):
        text = text[1:].strip()
    return " ".join(text.split())
