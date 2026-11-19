"""One résumé file in input/ is one person.

A single file keeps the original output/tailored_cvs layout. Two or more files
each get their own tailored CVs, application log, and browser profile.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from src.config import Settings
from src.cv_parser import extract_text
from src.errors import CVError
from src.models import Applicant, unique_slug

CV_SUFFIXES = {".pdf", ".docx", ".txt"}
_UNSUPPORTED = {".doc", ".rtf", ".pages", ".odt"}

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_LINKEDIN = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[^\s|<>]+", re.IGNORECASE)
_PORTFOLIO = re.compile(
    r"https?://(?!(?:www\.)?linkedin\.com\b)[^\s|<>]+",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\+\d{1,3}(?:[\s().-]*\d){7,14}")
_PHONE_LOCAL = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}")
_ROLE_WORDS = {
    "senior",
    "junior",
    "lead",
    "staff",
    "principal",
    "software",
    "developer",
    "engineer",
    "specialist",
    "manager",
    "analyst",
    "consultant",
    "director",
    "designer",
    "experience",
    "customer",
    "sales",
    "intern",
    "associate",
    "architect",
    "scientist",
    "officer",
}
_NOT_NAMES = {
    "about me",
    "professional summary",
    "work experience",
    "technical skills",
    "core skills",
    "selected projects",
    "personal projects",
}

_PROFILE_FIELDS = {
    "FULL_NAME": "full_name",
    "EMAIL": "email",
    "PHONE": "phone",
    "LINKEDIN_URL": "linkedin_url",
    "PORTFOLIO_URL": "portfolio_url",
    "LOCATION": "location",
    "WORK_AUTHORIZATION": "work_authorization",
    "SALARY_EXPECTATION": "salary_expectation",
}

@dataclass(frozen=True)
class PersonCV:
    """A source résumé plus the identity used to tailor and apply."""

    slug: str
    path: Path
    text: str
    applicant: Applicant
    solo: bool


@dataclass(frozen=True)
class PersonRun:
    """Where one person's tailored CVs, log, and browser session live."""

    slug: str
    manifest_path: Path
    log_dir: Path
    browser_profile_dir: Path


def discover_cvs(folder: Path) -> list[Path]:
    """Return résumé files in ``folder``, sorted by name."""
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in CV_SUFFIXES:
            found.append(path)
    return found


def unsupported_names(folder: Path) -> list[str]:
    """Résumé-like files the parser cannot read."""
    if not folder.is_dir():
        return []
    names = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in _UNSUPPORTED:
            names.append(path.name)
    return names


def profile_path_for(cv_path: Path) -> Path:
    """Sidecar that fills contact fields the résumé does not contain."""
    return cv_path.with_suffix(".profile")


def person_slug(path: Path, used: set[str]) -> str:
    """Filesystem slug from the résumé filename, without a trailing ``cv``."""
    stem = re.sub(r"[\s_-]*cv$", "", path.stem, flags=re.IGNORECASE).strip()
    normalized = unicodedata.normalize("NFKD", stem or path.stem)
    ascii_stem = "".join(char for char in normalized if not unicodedata.combining(char))
    return unique_slug(ascii_stem or "person", used)


def tailored_output_dir(slug: str, *, multiple: bool, output_dir: str) -> str:
    """Relative directory for this person's PDFs and manifest."""
    custom = output_dir != "output/tailored_cvs"
    if not multiple:
        return output_dir
    if custom:
        return str(Path(output_dir) / slug)
    return f"output/people/{slug}/tailored_cvs"


def person_runs(root: Path) -> list[PersonRun]:
    """Manifests written for more than one person."""
    folder = root / "output" / "people"
    if not folder.is_dir():
        return []
    runs = []
    for manifest in sorted(folder.glob("*/tailored_cvs/manifest.json")):
        slug = manifest.parents[1].name
        base = root / "output" / "people" / slug
        runs.append(
            PersonRun(
                slug=slug,
                manifest_path=manifest,
                log_dir=base / "logs",
                browser_profile_dir=base / "browser_profile",
            )
        )
    return runs


def load_people(folder: Path) -> list[PersonCV]:
    """Read every résumé in ``folder``. Contact details come from each file's text."""
    paths = discover_cvs(folder)
    solo = len(paths) == 1
    used: set[str] = set()
    people = []
    for path in paths:
        text = extract_text(path)
        people.append(
            PersonCV(
                slug=person_slug(path, used),
                path=path,
                text=text,
                applicant=resolve_applicant(text, profile_path_for(path)),
                solo=solo,
            )
        )
    return people


def resolve_applicant(
    text: str,
    profile_path: Path | None = None,
    shared: Applicant | None = None,
    *,
    solo: bool = True,
) -> Applicant:
    """Contact details for one résumé.

    Values already in the résumé stay. A ``.profile`` file fills whatever the
    résumé left blank. Nothing is copied from another person's profile.
    """
    del shared, solo
    extracted = extract_contact(text)
    sidecar = read_profile(profile_path) if profile_path else Applicant()
    return _fill_blanks(extracted, sidecar)


def applicant_for_run(
    stored: Applicant,
    source: Path,
    shared: Applicant | None = None,
) -> Applicant:
    """Refresh contact details from the résumé, then fill blanks from its sidecar."""
    del shared
    extracted = stored
    if source.is_file() and source.suffix.lower() in CV_SUFFIXES:
        try:
            extracted = extract_contact(extract_text(source))
        except CVError:
            extracted = stored
    sidecar = read_profile(profile_path_for(source)) if str(source) else Applicant()
    return _fill_blanks(extracted, sidecar)


def settings_for_person(settings: Settings, applicant: Applicant, browser_profile_dir: Path) -> Settings:
    """Copy of settings whose browser profile belongs to one person.

    ``applicant`` is not written into settings. Forms use the CV extract.
    """
    del applicant
    return settings.model_copy(update={"browser_profile_dir": str(browser_profile_dir)})


def extract_contact(text: str) -> Applicant:
    """Pull name and contact details from résumé text. Nothing is read from the environment."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = "\n".join(lines[:12])
    email_match = _EMAIL.search(text)
    linkedin_match = _LINKEDIN.search(text)
    portfolio_match = _PORTFOLIO.search(header) or _PORTFOLIO.search(text)
    return Applicant(
        full_name=_find_name(lines),
        email=email_match.group(0) if email_match else "",
        phone=_find_phone(header) or _find_phone(text),
        linkedin_url=_clean_url(linkedin_match.group(0) if linkedin_match else ""),
        portfolio_url=_clean_url(portfolio_match.group(0) if portfolio_match else ""),
        location=_find_location(lines),
        work_authorization=_find_line(
            text,
            ("authorized to work", "work authorization", "require sponsorship", "requires sponsorship"),
        ),
        salary_expectation=_find_line(text, ("salary expectation", "expected salary")),
    )


def _find_name(lines: list[str]) -> str:
    for line in lines[:6]:
        if _looks_like_name(line):
            return line.strip(" ·•-|")
    return ""


def _looks_like_name(line: str) -> bool:
    if any(token in line for token in "@|/\\") or "http" in line.lower() or any(char.isdigit() for char in line):
        return False
    if "·" in line or len(line) > 60:
        return False
    lowered = " ".join(line.lower().split())
    if lowered in _NOT_NAMES or lowered.startswith("about "):
        return False
    words = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", line, flags=re.UNICODE)
    if not 2 <= len(words) <= 4:
        return False
    if {word.lower() for word in words} & _ROLE_WORDS:
        return False
    return True


def _find_phone(header: str) -> str:
    match = _PHONE.search(header) or _PHONE_LOCAL.search(header)
    if match is None:
        return ""
    return " ".join(match.group(0).split())


def _find_location(lines: list[str]) -> str:
    for line in lines[:8]:
        if "|" not in line:
            continue
        for part in re.split(r"\s*\|\s*", line):
            if _EMAIL.search(part) or _LINKEDIN.search(part) or _find_phone(part):
                continue
            cleaned = part.strip(" ·•-|,")
            if not cleaned or len(cleaned) > 80:
                continue
            if "," in cleaned or cleaned.lower() in {"remote", "hybrid", "onsite", "on-site"}:
                return cleaned
            if not _looks_like_name(cleaned):
                return cleaned
    return ""


def _clean_url(value: str) -> str:
    cleaned = value.strip().strip("·•,;")
    if not cleaned:
        return ""
    cleaned = cleaned.split("?")[0].rstrip("/).,;")
    if cleaned.lower().startswith("http://"):
        cleaned = "https://" + cleaned[7:]
    elif not cleaned.lower().startswith("https://"):
        cleaned = "https://" + cleaned
    return cleaned


def _find_line(text: str, needles: tuple[str, ...]) -> str:
    for line in text.splitlines():
        cleaned = " ".join(line.split()).strip(" -•*")
        lowered = cleaned.lower()
        if cleaned and len(cleaned) <= 200 and any(needle in lowered for needle in needles):
            return cleaned
    return ""


def read_profile(path: Path) -> Applicant:
    """Read ``KEY=value`` lines. A missing file is an empty applicant."""
    if not path.is_file():
        return Applicant()
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CVError(f"Could not read {path.name}: {exc}") from exc
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        field = _PROFILE_FIELDS.get(key.strip().upper())
        if field is None:
            continue
        cleaned = value.strip().strip('"').strip("'")
        if cleaned:
            values[field] = cleaned
    return Applicant(**values)


def _fill_blanks(primary: Applicant, fallback: Applicant) -> Applicant:
    """Keep ``primary`` and use ``fallback`` only where ``primary`` is empty."""
    values = {}
    for field in _PROFILE_FIELDS.values():
        current = getattr(primary, field).strip()
        values[field] = current or getattr(fallback, field).strip()
    return Applicant(**values)


