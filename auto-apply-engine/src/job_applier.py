"""Search job boards and fill applications with the CV generated for that role.

The runner uses a persistent Playwright profile so the user can log in once.
When an application form is open, a short agent reads the visible controls,
labels each one, and corrects a label that does not fit the control. If that
agent fails, Chromium fills from the label text instead. If name, email, or
phone is still not among those controls, a second agent picks the right
control from the rest of the page. Fields that are still empty are inferred
from the profile and CV.
It pauses for sign-in, CAPTCHA, and any required field it cannot answer.
It does not solve CAPTCHAs or invent work authorization or salary.
"""

from __future__ import annotations

import html as html_lib
import json
import random
import re
import select
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from src.config import Settings
from src.cv_parser import extract_text
from src.errors import ConfigError, CVError, LLMError
from src.llm import LLMClient
from src.models import Applicant, JobFit, ManifestRole, ScreeningAnswer, TailoredCV
from src.tracking import ApplicationLog, ApplicationRecord, utc_now

JOB_LIMIT_SECONDS = 5 * 60


class _JobTimeLimit(Exception):
    """One job used its time. The run continues with the next job."""


class _SkipJob(Exception):
    """The person typed skip while a check was on screen."""


def _seconds_remaining(deadline: float, now: float) -> float:
    return max(0.0, deadline - now)


def _read_stdin(timeout: float) -> str | None:
    """Read one line, or return None when ``timeout`` seconds pass."""
    if timeout <= 0:
        return None
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    line = sys.stdin.readline()
    if line == "":
        return None
    return line


GENERIC_ROLE_WORDS = {
    "senior",
    "junior",
    "staff",
    "lead",
    "principal",
    "engineer",
    "developer",
    "manager",
    "remote",
    "full",
    "stack",
    "software",
    "the",
    "and",
}

LINK_SELECTORS = {
    "linkedin": [
        "a.job-card-container__link",
        "a[href*='/jobs/view/']",
    ],
    "indeed": [
        "a.jcs-JobTitle",
        "h2.jobTitle a",
        "a[id^='job_']",
        "a[href*='viewjob']",
        "a[href*='jk=']",
    ],
}

DESCRIPTION_SELECTORS = [
    "#job-description",
    ".jobs-description__content",
    ".jobs-box__html-content",
    "#jobDescriptionText",
    ".jobsearch-jobDescriptionText",
    ".job__description",
    "[data-qa='job-description']",
    "article",
]

COMPANY_SELECTORS = [
    ".job-details-jobs-unified-top-card__company-name",
    ".job-details-jobs-unified-top-card__company-name a",
    ".jobs-unified-top-card__company-name",
    ".jobs-unified-top-card__company-name a",
    ".topcard__org-name-link",
    "[data-testid='inlineHeader-companyName']",
    ".jobsearch-InlineCompanyRating-companyHeader",
    ".company-name",
]

APPLY_SELECTORS = [
    "button:has-text('Easy Apply')",
    "a:has-text('Easy Apply')",
    "button:has-text('Apply now')",
    "a:has-text('Apply now')",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "[data-testid='apply-button']",
]

NEXT_SELECTORS = [
    "button:has-text('Save and continue')",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Review')",
]

SUBMIT_SELECTORS = [
    "button:has-text('Submit application')",
    "button:has-text('Send application')",
    "button:has-text('Submit')",
    "input[type='submit']",
]

LINKEDIN_EASY_APPLY_SELECTORS = [
    ".jobs-apply-button--top-card",
    "button[data-live-test-job-apply-button]",
    ".jobs-s-apply button.jobs-apply-button",
    ".jobs-search__job-details button[aria-label*='Easy Apply']",
    ".jobs-details button[aria-label*='Easy Apply']",
    "button[aria-label*='Easy Apply']",
    "a[aria-label*='Easy Apply']",
    "button.jobs-apply-button",
]

LINKEDIN_MODAL_SELECTORS = (
    ".jobs-easy-apply-modal",
    "div[data-test-modal-id='easy-apply-modal']",
    ".jobs-easy-apply-content",
)

LINKEDIN_NEXT_SELECTORS = [
    "button[data-easy-apply-next-button]",
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "button[aria-label='Next']",
    "button[aria-label='Continue']",
    ".jobs-easy-apply-footer button",
    "footer button",
    "button.artdeco-button--primary",
    "button:has-text('Next')",
    "button:has-text('Review')",
    "button:has-text('Continue')",
]

_NEXT_BUTTON_NAME = re.compile(r"^(next|continue|review)\b", re.IGNORECASE)

# Page-level fallbacks. These stay specific so a jobs-list "Next" control is not clicked.
LINKEDIN_NEXT_PAGE_SELECTORS = [
    "button[data-easy-apply-next-button]",
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    ".jobs-easy-apply-modal footer button",
    ".jobs-easy-apply-content footer button",
    ".jobs-easy-apply-footer button",
]

LINKEDIN_SUBMIT_SELECTORS = [
    "button[aria-label='Submit application']",
    "button[aria-label*='Submit application']",
    "button[data-live-test-easy-apply-submit-button]",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    "footer button",
    "button.artdeco-button--primary",
]

LINKEDIN_SUBMIT_PAGE_SELECTORS = [
    "button[aria-label='Submit application']",
    "button[aria-label*='Submit application']",
    "button[data-live-test-easy-apply-submit-button]",
    "button:has-text('Submit application')",
]

_LINKEDIN_MODAL_ACTION_SELECTORS = [
    "footer button",
    ".jobs-easy-apply-footer button",
    ".artdeco-modal__actionbar button",
    "button[data-easy-apply-next-button]",
    "button[data-live-test-easy-apply-submit-button]",
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "button[aria-label='Submit application']",
    "button[aria-label*='Submit application']",
    "button.artdeco-button--primary",
    "button:has-text('Next')",
    "button:has-text('Review')",
    "button:has-text('Submit application')",
    "button",
]

_LINKEDIN_PAGE_ACTION_SELECTORS = [
    "button[data-easy-apply-next-button]",
    "button[data-live-test-easy-apply-submit-button]",
    "button[aria-label='Continue to next step']",
    "button[aria-label='Review your application']",
    "button[aria-label='Submit application']",
    "button[aria-label*='Submit application']",
    ".jobs-easy-apply-modal footer button",
    ".jobs-easy-apply-content footer button",
    ".jobs-easy-apply-footer button",
    ".artdeco-modal__actionbar button",
]

_HUMAN_TEXT = (
    "verify you are human",
    "are you a human",
    "are you a robot",
    "i'm not a robot",
    "im not a robot",
    "unusual traffic",
    "security check",
    "complete the captcha",
    "please verify",
)

_COUNTRIES = (
    "united states",
    "usa",
    "u.s.",
    "canada",
    "colombia",
    "united kingdom",
    "european union",
)

_SCORE_SYSTEM = """You compare a candidate CV with one job description.
Return one JSON object: {"score": 0, "summary": "", "dealbreakers": []}
score is an integer from 0 to 100 for how well the candidate's real experience matches the role.
summary is one sentence.
dealbreakers lists required licenses, degrees, or skills the CV does not show. An empty list is fine.
Do not assume the candidate has anything that is absent from the CV.
Nice-to-have skills are not dealbreakers.
Return JSON only.
"""

_ANSWER_SYSTEM = """You answer one job-application question from the candidate profile and CV.
Return one JSON object: {"answer": "", "needs_review": false}
Use only facts in the profile and CV. You may derive a fact the material supports, such as years of experience, current title, current employer, degree, or school.
Do not invent employers, dates, metrics, visa status, or salary.
If the material does not support an answer, set needs_review to true and answer to "".
Plain text only, no markdown.
Return JSON only.
"""

_INFER_SYSTEM = """You fill job-application fields that are still empty.
Return one JSON object: {"fields": [{"id": 0, "answer": ""}]}
The profile and CV are the only source. Derive an answer when the material supports it, such as years of experience, current title, current employer, degree, school, city, or a skill.
Do not invent employers, dates, metrics, visa status, or salary.
If the material does not support an answer, set answer to "".
When a field lists options, the answer must be exactly one of those options.
Include every id from the input. Plain text only. Return JSON only.
"""

_TAG_SYSTEM = """You label controls on a job application form that is already open in the browser.
Return one JSON object: {"fields": [{"id": 0, "tag": "email"}]}
Use only these tags:
first_name, last_name, full_name, email, phone, phone_prefix, linkedin, portfolio, city, country, location, school, degree, salary, work_authorization, cover_letter, eeo, sensitive, agreement, screening, resume, skip
Rules:
- Decide from the label, the control name, the nearby text, the type, and the options together.
- A box for the school, university, or college name is school. A degree or education-level box is degree. Graduation year and GPA are screening.
- "Location (City)" is city. A question about the country where the candidate currently resides is country.
- A question that only mentions email, phone, or a name is screening. "Years of experience with email marketing" is screening, not email.
- "First" or "Given name" is first_name. "Last", "Surname", or "Family name" is last_name. One "Name" box is full_name.
- A phone country-code or dial-code control is phone_prefix. The control immediately before the phone number is phone_prefix, even when its label says Country. Search boxes and buttons are skip.
- Gender, race, ethnicity, veteran, disability, and pronouns are eeo.
- Passwords, SSN, date of birth, and government ids are sensitive.
- Terms, privacy, and consent checkboxes are agreement.
- Resume or CV file inputs are resume.
- Every other question the candidate must answer is screening.
Include every id from the input exactly once. Do not add ids. Return JSON only.
"""

_RECOVERY_SYSTEM = """You find controls for contact fields that were not found on the form.
The missing tags are in "missing". The controls are the other fields on the open page.
Return one JSON object: {"fields": [{"id": 0, "tag": "email"}]}
Assign a missing tag only when that control is where the candidate types that fact.
Use "skip" for every control that is not one of the missing fields.
A question that only mentions a missing word is skip.
Do not invent ids or tags. Return JSON only.
"""

_EXTRA_CONTROL_SELECTOR = "[role='combobox'], [role='textbox'], [contenteditable='true'], [role='spinbutton']"

_PROFILE_TAGS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "email",
        "phone",
        "phone_prefix",
        "linkedin",
        "portfolio",
        "city",
        "country",
        "location",
        "school",
        "degree",
    }
)

_FIELD_TAGS = _PROFILE_TAGS | frozenset(
    {
        "salary",
        "work_authorization",
        "cover_letter",
        "eeo",
        "sensitive",
        "agreement",
        "screening",
        "resume",
        "skip",
    }
)

_SKIP_INPUT_TYPES = frozenset(
    {
        "input:hidden",
        "input:submit",
        "input:button",
        "input:search",
        "input:file",
        "input:password",
    }
)

_HELD_TAGS = _PROFILE_TAGS | frozenset({"salary", "work_authorization"})

_CORE_CONTACT_TAGS = frozenset({"first_name", "last_name", "full_name", "email", "phone"})


@dataclass
class JobPosting:
    title: str
    company: str
    url: str
    description: str
    board: str


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _HTMLText()
    parser.feed(value or "")
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def canonical_url(url: str) -> str:
    """Drop tracking parameters while keeping the job id."""
    raw = (url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    query = parse_qs(parsed.query, keep_blank_values=False)
    if "jk" in query:
        kept = urlencode({"jk": query["jk"][0]})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", kept, ""))
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "trackingid", "trk", "refid", "position", "pagenum", "from"}
    kept_pairs = []
    for key, values in query.items():
        if key.lower() in drop:
            continue
        for value in values:
            kept_pairs.append((key, value))
    kept = urlencode(kept_pairs)
    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme, parsed.netloc, path, "", kept, ""))


def keyword_match(title: str, description: str, keywords: list[str], role_title: str) -> bool:
    haystack = f"{title}\n{description}".lower()
    title_l = title.lower()
    for keyword in keywords:
        term = keyword.strip().lower()
        if len(term) >= 3 and term in haystack:
            return True
    for word in re.findall(r"[a-z0-9+#]+", role_title.lower()):
        if len(word) >= 4 and word not in GENERIC_ROLE_WORDS and word in title_l:
            return True
    return False


def needs_human(url: str, text: str) -> str | None:
    blob = f"{url}\n{text[:2500]}".lower()
    if any(marker in blob for marker in _HUMAN_TEXT):
        return "The site is showing a human verification check. Complete it in the browser, then continue."
    lowered = url.lower()
    if any(part in lowered for part in ("/login", "/uas/login", "signin", "sign-in", "/checkpoint/")):
        return "Sign in is required. Log in in the browser window. This profile is saved for the next run."
    return None


def is_eeo_question(label: str) -> bool:
    return re.search(
        r"\b(gender|race|ethnic|ethnicity|veteran|disability|pronouns?|sexual orientation|self-identify)\b",
        label,
        re.IGNORECASE,
    ) is not None


def is_sensitive_question(label: str) -> bool:
    return re.search(
        r"social security|\bssn\b|credit card|\bcvv\b|\bpassword\b|date of birth|driver'?s license|\bnational id\b",
        label,
        re.IGNORECASE,
    ) is not None


def is_salary_question(label: str) -> bool:
    return re.search(r"\b(salary|compensation|pay expectation|desired pay|expected pay)\b", label, re.IGNORECASE) is not None


def is_agreement_question(label: str) -> bool:
    return re.search(
        r"\b(i agree|terms of service|privacy policy|terms and conditions|consent to)\b",
        label,
        re.IGNORECASE,
    ) is not None


def is_work_auth_question(label: str) -> bool:
    text = label.lower()
    if re.search(r"\bvisa\b", text):
        return True
    return any(
        token in text
        for token in (
            "sponsor",
            "work authorization",
            "work authorisation",
            "authorized to work",
            "authorised to work",
            "legally authorized",
            "legally authorised",
            "eligible to work",
        )
    )


def yes_no_answer(question: str, authorization: str) -> str | None:
    """Map a configured work-authorization sentence onto Yes or No. Returns None when unsure."""
    question_l = question.lower()
    authorization_l = authorization.lower().strip()
    if not authorization_l:
        return None
    sponsorship = "sponsor" in question_l
    authorized = any(
        token in question_l
        for token in (
            "authorized",
            "authorised",
            "eligible to work",
            "legally authorized",
            "work authorization",
            "work authorisation",
        )
    )
    if not sponsorship and not authorized:
        return None

    question_countries = _countries_in(question_l)
    answer_countries = _countries_in(authorization_l)
    if question_countries and answer_countries and question_countries.isdisjoint(answer_countries):
        return None

    no_sponsor = any(
        phrase in authorization_l
        for phrase in (
            "no sponsorship",
            "do not require sponsorship",
            "don't require sponsorship",
            "does not require sponsorship",
            "without sponsorship",
            "not require sponsorship",
        )
    )
    needs_sponsor = (not no_sponsor) and any(
        phrase in authorization_l
        for phrase in (
            "require sponsorship",
            "requires sponsorship",
            "need sponsorship",
            "needs sponsorship",
        )
    )
    if sponsorship:
        if no_sponsor:
            return "No"
        if needs_sponsor:
            return "Yes"
        return None
    if "not authorized" in authorization_l or "not authorised" in authorization_l:
        return "No"
    if any(token in authorization_l for token in ("authorized", "authorised", "eligible")):
        return "Yes"
    return None


def _humanize_control_name(name: str) -> str:
    """Turn ``job_application[first_name]`` into ``first name``."""
    match = re.search(r"\[([^\]]+)\]", name or "")
    text = match.group(1) if match else (name or "")
    if not text or text.startswith("field_") or text.startswith(":"):
        return ""
    return _label_text(text)


def _label_text(label: str) -> str:
    """Lowercase a label, and split first_name / firstName into separate words."""
    text = label.replace("*", " ").replace("_", " ").replace("-", " ")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"linked\s+in", "linkedin", text, flags=re.IGNORECASE)
    return " ".join(text.lower().split())


def _looks_like_city_field(text: str) -> bool:
    """A box for the city, such as Location (City), not a question that mentions a city."""
    if not text or "?" in text or len(text) > 40:
        return False
    if "authorization" in text or "authorisation" in text:
        return False
    return "city" in text


def _looks_like_country_field(text: str) -> bool:
    """The country where the candidate lives, not a phone calling code."""
    if not text or _looks_like_phone_prefix_field(text):
        return False
    if any(token in text for token in ("authoriz", "authoris", "sponsor", "visa", "citizen")):
        return False
    if text == "country":
        return True
    return "country" in text and any(
        token in text for token in ("reside", "residence", "live", "living", "located", "where you")
    )


def _looks_like_degree_field(text: str) -> bool:
    """A degree or education-level box, not GPA, major, or graduation year."""
    if not text or "?" in text or len(text) > 60:
        return False
    if re.search(r"\b(gpa|graduat\w*|major|field of study)\b", text):
        return False
    return re.search(r"\b(degree|education level|level of education|highest level)\b", text) is not None


def _looks_like_school_field(text: str) -> bool:
    """A box for the institution name, not a degree, GPA, or graduation question."""
    if not text or len(text) > 80:
        return False
    if "?" in text and not re.search(r"\b(school|university|college|institution)\b", text):
        return False
    if re.search(
        r"\b(gpa|diploma|degree|graduat\w*|major|field of study|education level|level of education|highest)\b",
        text,
    ):
        if not re.search(
            r"\b(school name|university name|college name|institution name|name of (?:the )?(?:school|university|college))\b",
            text,
        ):
            return False
    return re.search(r"\b(school|university|college|institution|alma mater)\b", text) is not None


def profile_value_for_label(label: str, applicant: Applicant) -> str | None:
    text = _label_text(label)
    if not text:
        return None
    if "given name" in text or (re.search(r"\bfirst\b", text) and "name" in text):
        return applicant.first_name or None
    if any(token in text for token in ("surname", "family name")) or (re.search(r"\blast\b", text) and "name" in text):
        return applicant.last_name or None
    if _looks_like_email_field(text):
        return applicant.email or None
    if _looks_like_phone_prefix_field(text):
        return applicant.phone_prefix or None
    if _looks_like_country_field(text):
        return _country_from_location(applicant.location)
    if _looks_like_city_field(text):
        return _city_from_location(applicant.location) or applicant.location or None
    if _looks_like_phone_field(text):
        return applicant.phone_number or applicant.phone or None
    if "linkedin" in text:
        return applicant.linkedin_url or None
    if any(token in text for token in ("portfolio", "website", "github")) or (
        re.search(r"\burl\b", text) and "job" not in text
    ):
        return applicant.portfolio_url or None
    if re.search(r"\b(city|location)\b", text) and "authorization" not in text:
        return applicant.location or None
    if _looks_like_school_field(text):
        return applicant.school or None
    if _looks_like_degree_field(text):
        return applicant.degree or None
    if text in {"name", "name *"} or re.search(r"\b(full name|your name)\b", text):
        return applicant.full_name or None
    return None


def _looks_like_email_field(text: str) -> bool:
    """True for a contact box, false for a question that only mentions email."""
    if "?" in text or len(text) > 60:
        return False
    if re.search(r"\b(marketing|campaign|experience|years|skill|skills)\b", text):
        return False
    return "email" in text or "e-mail" in text


def _looks_like_phone_prefix_field(text: str) -> bool:
    """A calling-code box, such as Country code or Dial code, not the phone number."""
    if "?" in text or len(text) > 60:
        return False
    return any(
        token in text
        for token in ("country code", "dial code", "calling code", "phone code", "phone prefix")
    )


def _looks_like_phone_field(text: str) -> bool:
    if "?" in text or len(text) > 60:
        return False
    if _looks_like_phone_prefix_field(text):
        return False
    if re.search(r"\b(interview|screening)\b", text):
        return False
    return "phone" in text or "mobile" in text


def value_for_tag(tag: str, applicant: Applicant) -> str | None:
    """Profile text for a tag the model assigned to a live form control."""
    city = _city_from_location(applicant.location) or ""
    country = _country_from_location(applicant.location) or ""
    values = {
        "first_name": applicant.first_name,
        "last_name": applicant.last_name,
        "full_name": applicant.full_name,
        "email": applicant.email,
        "phone": applicant.phone_number or applicant.phone,
        "phone_prefix": applicant.phone_prefix,
        "linkedin": applicant.linkedin_url,
        "portfolio": applicant.portfolio_url,
        "city": city,
        "country": country,
        "location": applicant.location,
        "school": applicant.school,
        "degree": applicant.degree,
        "salary": applicant.salary_expectation,
        "work_authorization": applicant.work_authorization,
    }
    value = (values.get(tag) or "").strip()
    return value or None


def profile_value_for_field(tag: str | None, label: str, applicant: Applicant) -> str | None:
    """Fill from the model tag. Keyword matching runs only when the control was not labeled."""
    if tag in {"skip", "resume", "eeo", "sensitive", "agreement", "screening", "cover_letter"}:
        return None
    if tag:
        return value_for_tag(tag, applicant)
    return profile_value_for_label(label, applicant)


def form_tag_requests(
    radios: dict[str, list[dict[str, Any]]],
    others: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compact snapshot of the empty controls on the open form."""
    rows: list[dict[str, Any]] = []

    def add(kind: str, key: Any, payload: dict[str, Any]) -> None:
        payload["id"] = len(rows)
        payload["kind"] = kind
        payload["key"] = key
        rows.append(payload)

    for name, group in radios.items():
        if _already_checked(group):
            continue
        question = group[0].get("group_label") or group[0].get("label") or name
        choices = [item.get("choice") or item.get("label") or "" for item in group]
        add(
            "radio",
            name,
            {
                "label": _clip(str(question), 300),
                "type": "radio",
                "choices": [_clip(str(item), 80) for item in choices[:12]],
                "required": any(item.get("required") for item in group),
                "context": _clip(str(group[0].get("context") or ""), 280),
            },
        )
    for index, field in enumerate(others):
        field_type = str(field.get("type") or "")
        if field_type in _SKIP_INPUT_TYPES:
            continue
        if _has_value(field):
            continue
        add(
            "field",
            index,
            {
                "label": _clip(str(field.get("label") or ""), 300),
                "group": _clip(str(field.get("group_label") or ""), 200),
                "name": _clip(str(field.get("name") or ""), 120),
                "type": field_type,
                "required": bool(field.get("required")),
                "options": [_clip(str(option), 80) for option in (field.get("options") or [])[:12]],
                "context": _clip(str(field.get("context") or ""), 280),
            },
        )
    return rows[:40]


def parse_field_tags(payload: dict[str, Any], valid_ids: set[int]) -> dict[int, str]:
    """Keep tags the model returned for ids that are actually on the form."""
    tags: dict[int, str] = {}
    items = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return tags
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            field_id = int(item.get("id", item.get("index")))
        except (TypeError, ValueError):
            continue
        tag = str(item.get("tag") or "").strip().lower().replace(" ", "_").replace("-", "_")
        if field_id not in valid_ids or tag not in _FIELD_TAGS:
            continue
        tags[field_id] = tag
    return tags


_TYPED_CONTACT_TAGS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "email",
        "phone",
        "phone_prefix",
        "linkedin",
        "portfolio",
        "city",
        "location",
        "school",
        "salary",
    }
)

_YES_NO = frozenset({"yes", "no", "y", "n", "true", "false"})


def tag_conflict(tag: str, row: dict[str, Any]) -> str | None:
    """Return why this tag cannot belong on this control."""
    field_type = str(row.get("type") or "")
    options = [str(item).strip().lower() for item in (row.get("options") or row.get("choices") or [])]
    yes_no = bool(options) and all(item in _YES_NO or item.startswith(("yes", "no")) for item in options)
    if tag in _TYPED_CONTACT_TAGS and field_type == "radio":
        return "a radio group is a question, not a box you type a contact into"
    if tag in _TYPED_CONTACT_TAGS and field_type.startswith("input:checkbox"):
        return "a checkbox is not a box you type a contact into"
    if tag in _TYPED_CONTACT_TAGS and field_type == "select" and yes_no:
        return "a yes/no menu is not a contact field"
    if tag == "cover_letter" and (field_type in {"radio", "select"} or field_type.startswith("input:checkbox")):
        return "a cover letter is a text box"
    if tag == "resume":
        return "resume uploads are filled separately"
    return None


def _visible_fields(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visible = []
    for row in requests:
        item = {
            key: value
            for key, value in row.items()
            if key not in {"kind", "key", "field", "field_ref"} and value not in ("", [], None)
        }
        item["id"] = row["id"]
        visible.append(item)
    return visible


def _conflict_notes(tags: dict[int, str], requests: list[dict[str, Any]]) -> list[str]:
    by_id = {row["id"]: row for row in requests}
    notes = []
    for field_id, tag in tags.items():
        reason = tag_conflict(tag, by_id.get(field_id) or {})
        if reason:
            notes.append(f"id {field_id} tagged {tag}: {reason}")
    return notes


def _without_conflicts(tags: dict[int, str], requests: list[dict[str, Any]]) -> dict[int, str]:
    by_id = {row["id"]: row for row in requests}
    return {
        field_id: tag
        for field_id, tag in tags.items()
        if field_id in by_id and not tag_conflict(tag, by_id[field_id])
    }


def run_form_tag_agent(llm: Any, requests: list[dict[str, Any]]) -> dict[int, str]:
    """Label the open form, check those labels, and correct them once.

    An empty result means the agent failed. The caller fills from the label text.
    """
    if not requests:
        return {}
    valid_ids = {row["id"] for row in requests}
    user = json.dumps({"fields": _visible_fields(requests)}, ensure_ascii=False)
    payload = llm.complete_json(_TAG_SYSTEM, user, temperature=0, max_tokens=1500)
    tags = parse_field_tags(payload, valid_ids)
    notes = _conflict_notes(tags, requests)
    if notes:
        correction = (
            user
            + "\n\nYour previous labels:\n"
            + json.dumps({"fields": [{"id": field_id, "tag": tag} for field_id, tag in sorted(tags.items())]})
            + "\n\nThese labels do not fit the control. Label every id again and fix these:\n"
            + "\n".join(f"- {note}" for note in notes)
        )
        payload = llm.complete_json(_TAG_SYSTEM, correction, temperature=0, max_tokens=1500)
        tags = parse_field_tags(payload, valid_ids)
    return _without_conflicts(tags, requests)


def label_profile_tag(label: str) -> str | None:
    """The contact tag a label names, when the label is a contact box."""
    text = _label_text(label)
    if not text:
        return None
    if "given name" in text or (re.search(r"\bfirst\b", text) and "name" in text):
        return "first_name"
    if any(token in text for token in ("surname", "family name")) or (re.search(r"\blast\b", text) and "name" in text):
        return "last_name"
    if _looks_like_email_field(text):
        return "email"
    if _looks_like_phone_prefix_field(text):
        return "phone_prefix"
    if _looks_like_country_field(text):
        return "country"
    if _looks_like_city_field(text):
        return "city"
    if _looks_like_phone_field(text):
        return "phone"
    if "linkedin" in text:
        return "linkedin"
    if any(token in text for token in ("portfolio", "website", "github")) or (
        re.search(r"\burl\b", text) and "job" not in text
    ):
        return "portfolio"
    if re.search(r"\blocation\b", text) and "authorization" not in text:
        return "location"
    if _looks_like_school_field(text):
        return "school"
    if _looks_like_degree_field(text):
        return "degree"
    if text in {"name", "name *"} or re.search(r"\b(full name|your name)\b", text):
        return "full_name"
    if is_salary_question(label):
        return "salary"
    if is_work_auth_question(label):
        return "work_authorization"
    return None


_PHONE_PREFIX_KEEP = frozenset({"first_name", "last_name", "full_name", "email", "resume"})


def _is_phone_control(field: dict[str, Any]) -> bool:
    """A phone-number box, not the calling-code box beside it."""
    if str(field.get("tag") or "") == "phone":
        return True
    if str(field.get("tag") or "") == "phone_prefix":
        return False
    label = _label_text(str(field.get("label") or ""))
    group = _label_text(str(field.get("group_label") or ""))
    return _looks_like_phone_field(label) or _looks_like_phone_field(group)


def mark_phone_prefix_before_phone(fields: list[dict[str, Any]]) -> None:
    """The control directly before the phone number is the calling code."""
    eligible = [
        field
        for field in fields
        if str(field.get("type") or "") not in _SKIP_INPUT_TYPES
    ]
    for index, field in enumerate(eligible):
        if index == 0 or not _is_phone_control(field):
            continue
        previous = eligible[index - 1]
        if _is_phone_control(previous):
            continue
        if str(previous.get("tag") or "") in _PHONE_PREFIX_KEEP:
            continue
        previous["tag"] = "phone_prefix"


def found_contact_tags(fields: list[dict[str, Any]]) -> set[str]:
    """Contact tags already sitting on a control, by tag or by its label."""
    found: set[str] = set()
    for field in fields:
        tag = str(field.get("tag") or "")
        if tag in _HELD_TAGS:
            found.add(tag)
            continue
        if tag in {"skip", "screening", "eeo", "sensitive", "agreement", "cover_letter", "resume"}:
            continue
        inferred = label_profile_tag(str(field.get("label") or "")) or label_profile_tag(str(field.get("group_label") or ""))
        if inferred:
            found.add(inferred)
    return found


def missing_contact_tags(found: set[str], applicant: Applicant) -> list[str]:
    """Contact facts that have a value and no control yet."""
    missing: list[str] = []
    if "full_name" not in found and "first_name" not in found and value_for_tag("first_name", applicant):
        missing.append("first_name")
    if "full_name" not in found and "last_name" not in found and value_for_tag("last_name", applicant):
        missing.append("last_name")
    if "full_name" not in found and "first_name" not in found and value_for_tag("full_name", applicant):
        missing.append("full_name")
    for tag in (
        "email",
        "phone",
        "phone_prefix",
        "linkedin",
        "portfolio",
        "city",
        "country",
        "location",
        "school",
        "salary",
        "work_authorization",
    ):
        if tag in found or not value_for_tag(tag, applicant):
            continue
        missing.append(tag)
    return missing


def _is_recovery_candidate(field: dict[str, Any]) -> bool:
    tag = str(field.get("tag") or "")
    if tag in _HELD_TAGS or tag in {"eeo", "sensitive", "agreement", "resume"}:
        return False
    if str(field.get("type") or "") in _SKIP_INPUT_TYPES:
        return False
    try:
        if _has_value(field):
            return False
    except Exception:
        return False
    return True


def recovery_requests(
    radios: dict[str, list[dict[str, Any]]],
    others: list[dict[str, Any]],
    extras: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Controls the first pass did not accept as a contact field."""
    rows: list[dict[str, Any]] = []

    def add(kind: str, field: dict[str, Any]) -> None:
        if len(rows) >= 40 or not _is_recovery_candidate(field):
            return
        choices = field.get("options") or []
        rows.append(
            {
                "id": len(rows),
                "kind": kind,
                "field": field,
                "label": _clip(str(field.get("group_label") or field.get("label") or ""), 300),
                "name": _clip(str(field.get("name") or ""), 120),
                "type": str(field.get("type") or ""),
                "required": bool(field.get("required")),
                "options": [_clip(str(option), 80) for option in choices[:12]],
                "context": _clip(str(field.get("context") or ""), 280),
            }
        )

    for group in radios.values():
        if not group or _already_checked(group):
            continue
        sample = dict(group[0])
        sample["type"] = "radio"
        sample["label"] = group[0].get("group_label") or group[0].get("label") or ""
        sample["options"] = [item.get("choice") or item.get("label") or "" for item in group]
        sample["field_ref"] = group[0]
        before = len(rows)
        add("radio", sample)
        if len(rows) > before:
            rows[-1]["field"] = group[0]
    for field in others:
        add("field", field)
    for field in extras:
        add("extra", field)
    return rows


def _recovery_tags(payload: dict[str, Any], valid_ids: set[int], allowed: set[str]) -> dict[int, str]:
    tags = parse_field_tags(payload, valid_ids)
    return {field_id: tag for field_id, tag in tags.items() if tag in allowed}


def run_control_recovery_agent(
    llm: Any,
    requests: list[dict[str, Any]],
    missing: list[str],
) -> dict[int, str]:
    """Pick controls for contact fields the first pass did not find.

    Tags that are not missing, or that do not fit the control, are dropped.
    """
    allowed = set(missing)
    if not requests or not allowed:
        return {}
    valid_ids = {row["id"] for row in requests}
    user = json.dumps(
        {"missing": missing, "fields": _visible_fields(requests)},
        ensure_ascii=False,
    )
    payload = llm.complete_json(_RECOVERY_SYSTEM, user, temperature=0, max_tokens=1500)
    tags = _recovery_tags(payload, valid_ids, allowed)
    notes = _conflict_notes(tags, requests)
    if notes:
        correction = (
            user
            + "\n\nThese labels do not fit the control. Label every id again and fix these:\n"
            + "\n".join(f"- {note}" for note in notes)
        )
        payload = llm.complete_json(_RECOVERY_SYSTEM, correction, temperature=0, max_tokens=1500)
        tags = _recovery_tags(payload, valid_ids, allowed)
    return _without_conflicts(tags, requests)


_LEAVE_BLANK_TAGS = frozenset({"eeo", "sensitive", "agreement", "skip", "resume"})


def infer_field_requests(
    radios: dict[str, list[dict[str, Any]]],
    others: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Empty controls that still need an answer from the profile."""
    rows: list[dict[str, Any]] = []

    def add(kind: str, key: Any, field: dict[str, Any], payload: dict[str, Any]) -> None:
        payload["id"] = len(rows)
        payload["kind"] = kind
        payload["key"] = key
        payload["field"] = field
        rows.append(payload)

    for name, group in radios.items():
        if not group or _already_checked(group):
            continue
        tag = str(group[0].get("tag") or "")
        question = str(group[0].get("group_label") or group[0].get("label") or name)
        if tag in _LEAVE_BLANK_TAGS or _leave_blank(question):
            continue
        choices = [item.get("choice") or item.get("label") or "" for item in group]
        add(
            "radio",
            name,
            group[0],
            {
                "label": _clip(question, 300),
                "type": "radio",
                "options": [_clip(str(item), 80) for item in choices[:12]],
                "required": any(item.get("required") for item in group),
                "context": _clip(str(group[0].get("context") or ""), 280),
            },
        )
    for field in others:
        field_type = str(field.get("type") or "")
        if field_type in _SKIP_INPUT_TYPES:
            continue
        try:
            if _has_value(field):
                continue
        except Exception:
            continue
        label = str(field.get("group_label") or field.get("label") or "")
        tag = str(field.get("tag") or "")
        if tag in _LEAVE_BLANK_TAGS or _leave_blank(label):
            continue
        if re.search(r"\bfollow\b", label, re.IGNORECASE) and not field.get("required"):
            continue
        add(
            "field",
            None,
            field,
            {
                "label": _clip(str(field.get("label") or ""), 300),
                "group": _clip(str(field.get("group_label") or ""), 200),
                "name": _clip(str(field.get("name") or ""), 120),
                "type": field_type,
                "required": bool(field.get("required")),
                "options": [_clip(str(option), 80) for option in (field.get("options") or [])[:12]],
                "context": _clip(str(field.get("context") or ""), 280),
            },
        )
    rows.sort(key=lambda row: not row["required"])
    for index, row in enumerate(rows[:30]):
        row["id"] = index
    return rows[:30]


def _leave_blank(label: str) -> bool:
    return is_eeo_question(label) or is_sensitive_question(label) or is_agreement_question(label)


def parse_inferred_answers(payload: dict[str, Any], valid_ids: set[int]) -> dict[int, str]:
    """Keep non-empty answers for ids that were actually sent."""
    answers: dict[int, str] = {}
    items = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return answers
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            field_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if field_id not in valid_ids:
            continue
        answer = " ".join(str(item.get("answer") or "").replace("\n", " ").split())
        if answer:
            answers[field_id] = answer
    return answers


def _option_mismatches(answers: dict[int, str], requests: list[dict[str, Any]]) -> list[str]:
    by_id = {row["id"]: row for row in requests}
    notes = []
    for field_id, answer in answers.items():
        options = by_id.get(field_id, {}).get("options") or []
        if options and not _option_match(answer, options):
            notes.append(f"id {field_id} answer {answer!r} is not one of the options")
    return notes


def _answers_on_options(answers: dict[int, str], requests: list[dict[str, Any]]) -> dict[int, str]:
    by_id = {row["id"]: row for row in requests}
    kept: dict[int, str] = {}
    for field_id, answer in answers.items():
        options = by_id.get(field_id, {}).get("options") or []
        if options and not _option_match(answer, options):
            continue
        kept[field_id] = answer
    return kept


def run_profile_infer_agent(
    llm: Any,
    requests: list[dict[str, Any]],
    profile_text: str,
    cv_text: str,
) -> dict[int, str]:
    """Infer answers for empty fields from the profile and CV.

    An answer that is not one of a field's options is sent back once, then dropped.
    """
    if not requests:
        return {}
    valid_ids = {row["id"] for row in requests}
    user = json.dumps(
        {
            "profile": profile_text,
            "cv": cv_text[:8000],
            "fields": _visible_fields(requests),
        },
        ensure_ascii=False,
    )
    payload = llm.complete_json(_INFER_SYSTEM, user, temperature=0, max_tokens=2000)
    answers = parse_inferred_answers(payload, valid_ids)
    notes = _option_mismatches(answers, requests)
    if notes:
        correction = (
            user
            + "\n\nThese answers are not one of the listed options. Answer every id again and fix these:\n"
            + "\n".join(f"- {note}" for note in notes)
        )
        payload = llm.complete_json(_INFER_SYSTEM, correction, temperature=0, max_tokens=2000)
        answers = parse_inferred_answers(payload, valid_ids)
    return _answers_on_options(answers, requests)


def _inferred_limit(row: dict[str, Any]) -> int:
    label = str(row.get("label") or "")
    field_type = str(row.get("type") or "")
    if field_type == "textarea" or re.search(r"cover letter|why (do you|are you)", label, re.IGNORECASE):
        return 1500
    return 500


def apply_inferred_answers(
    radios: dict[str, list[dict[str, Any]]],
    requests: list[dict[str, Any]],
    answers: dict[int, str],
) -> set[str]:
    """Type inferred answers. Returns the labels that accepted a value."""
    filled: set[str] = set()
    for row in requests:
        answer = answers.get(row["id"])
        if not answer:
            continue
        label = str(row.get("label") or "")
        if row["kind"] == "radio":
            group = radios.get(row["key"]) or []
            choices = [item.get("choice") or item.get("label") or "" for item in group]
            matched = _option_match(answer, choices)
            if matched and _check_matching(group, matched):
                filled.add(label)
            continue
        field = row["field"]
        field_type = str(field.get("type") or "")
        text = answer[: _inferred_limit(row)]
        written = False
        if field_type == "select":
            written = _select_choice(field, text)
        elif field_type.startswith("input:checkbox"):
            if text.strip().lower() in {"yes", "true", "y"}:
                written = _write(field, "yes")
        else:
            written = _write(field, text)
        if written:
            filled.add(str(field.get("label") or label))
    return {item for item in filled if item}


def _without_filled(unresolved: list[str], filled_labels: set[str]) -> list[str]:
    kept = []
    for item in unresolved:
        if any(item == label or item.endswith(f": {label}") for label in filled_labels):
            continue
        kept.append(item)
    return kept


def assign_form_tags(
    radios: dict[str, list[dict[str, Any]]],
    others: list[dict[str, Any]],
    tags_by_id: dict[int, str],
    requests: list[dict[str, Any]],
) -> None:
    for row in requests:
        tag = tags_by_id.get(row["id"])
        if not tag:
            continue
        if row["kind"] == "radio":
            for item in radios.get(row["key"], []):
                item["tag"] = tag
            continue
        others[row["key"]]["tag"] = tag


def _clip(value: str, limit: int) -> str:
    return _clean(value)[:limit]


def _location_parts(location: str) -> list[str]:
    return [part.strip() for part in (location or "").split(",") if part.strip()]


def _city_from_location(location: str) -> str | None:
    parts = _location_parts(location)
    return parts[0] if parts else None


def _country_from_location(location: str) -> str | None:
    parts = _location_parts(location)
    if len(parts) >= 2:
        return parts[-1]
    return None


def _countries_in(text: str) -> set[str]:
    found: set[str] = set()
    for country in _COUNTRIES:
        if " " in country or "." in country:
            if country in text:
                found.add(country)
            continue
        if re.search(rf"\b{re.escape(country)}\b", text):
            found.add(country)
    return found


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _accept_apply(label: str) -> bool:
    text = label.lower()
    if any(bad in text for bad in ("filter", "alert", "search", "save", "notification")):
        return False
    return "apply" in text


def _accept_easy_apply(label: str) -> bool:
    return "easy apply" in label.lower()


def _accept_next(label: str) -> bool:
    text = " ".join(label.lower().split())
    if not text:
        return False
    if any(bad in text for bad in ("next page", "previous", "search jobs", "job alert", "see more jobs")):
        return False
    if any(phrase in text for phrase in ("save and continue", "next step", "review your application", "continue applying")):
        return True
    return text.split()[0] in {"continue", "next", "review"}


def _accept_submit(label: str) -> bool:
    text = " ".join(label.lower().split())
    if not text or "search" in text or "submitted" in text:
        return False
    return "submit" in text or "send application" in text


def _control_label(element: Any) -> str:
    parts: list[str] = []
    for attr in ("aria-label", "value"):
        try:
            parts.append(element.get_attribute(attr) or "")
        except Exception:
            continue
    try:
        parts.append(element.inner_text(timeout=500))
    except Exception:
        pass
    return _clean(" ".join(parts))


def _is_disabled(element: Any) -> bool:
    try:
        if element.get_attribute("disabled") is not None:
            return True
        if str(element.get_attribute("aria-disabled")).lower() == "true":
            return True
        return "artdeco-button--disabled" in (element.get_attribute("class") or "")
    except Exception:
        return False


class JobApplier:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        log: ApplicationLog,
        console: Console | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.log = log
        self.console = console or Console()
        self.headless = settings.headless
        self._progress: Any = None
        self._linkedin_login_skipped = False
        self._applying = False

    def run(
        self,
        roles: list[ManifestRole],
        *,
        limit: int,
        dry_run: bool,
        auto_submit: bool,
        boards: list[str],
        threshold: int,
        urls_file: Path | None = None,
        applicant: Applicant | None = None,
    ) -> list[ApplicationRecord]:
        if limit < 1:
            raise ConfigError("--limit must be at least 1.")
        applicant = applicant or Applicant()
        if not dry_run:
            missing = [name for name, value in (("name", applicant.full_name), ("email", applicant.email)) if not value]
            if missing:
                raise ConfigError(
                    "This résumé is missing "
                    + " and ".join(missing)
                    + ". Add them to the résumé, or set them in the .profile file next to it."
                )
        outcomes: list[ApplicationRecord] = []
        sync_playwright = _load_playwright()
        profile_dir = self.settings.resolve(self.settings.browser_profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=self.headless,
                    viewport={"width": 1366, "height": 900},
                    accept_downloads=True,
                )
            except Exception as exc:
                message = str(exc).lower()
                if "executable" in message or "browsertype.launch" in message:
                    raise ConfigError(
                        "Playwright Chromium is not installed. Run: playwright install chromium"
                    ) from exc
                raise
            page = context.pages[0] if context.pages else context.new_page()
            page.on("dialog", _accept_browser_dialog)
            eligible = 0
            try:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    console=self.console,
                ) as progress:
                    self._progress = progress
                    for role in roles:
                        if eligible >= limit:
                            break
                        cv_text = _role_context(role, self.settings)
                        pdf_path = self.settings.resolve(role.pdf_path)
                        if not pdf_path.exists():
                            raise CVError(f"Tailored PDF not found for {role.title}: {pdf_path}")
                        jobs = self.discover(page, role, boards, max(limit * 4, 8), urls_file)
                        if not jobs:
                            progress.console.print(f"[yellow]No jobs found for {role.title}.[/yellow]")
                            continue
                        task = progress.add_task(role.title, total=len(jobs))
                        for job in jobs:
                            if eligible >= limit:
                                progress.advance(task)
                                continue
                            if self.log.is_blocked(job.url):
                                progress.console.print(f"[dim]Skipping already logged {job.url}[/dim]")
                                progress.advance(task)
                                continue
                            self._sleep_short()
                            record = self.process_job(
                                page,
                                job,
                                role,
                                applicant,
                                cv_text,
                                pdf_path,
                                dry_run=dry_run,
                                auto_submit=auto_submit,
                                threshold=threshold,
                            )
                            wrote = self.log.record(record)
                            if wrote:
                                outcomes.append(record)
                            score = "-" if record.match_score is None else f"{record.match_score:.0f}"
                            progress.console.print(
                                f"  {record.status:<18} {score:>5}  {record.job_title} @ {record.company}"
                            )
                            progress.advance(task)
                            if record.status in {"submitted", "dry_run", "pending_review"}:
                                eligible += 1
                                if record.status != "dry_run":
                                    self._sleep_between()
            finally:
                self._progress = None
                try:
                    context.close()
                except Exception:
                    pass
        return outcomes

    def discover(
        self,
        page: Any,
        role: ManifestRole,
        boards: list[str],
        cap: int,
        urls_file: Path | None,
    ) -> list[JobPosting]:
        if urls_file is not None:
            return _jobs_from_file(urls_file, role)
        found: list[JobPosting] = []
        seen: set[str] = set()
        per_board = max(cap, 5)
        for board in boards:
            try:
                if board == "greenhouse":
                    batch = self._discover_greenhouse(role, per_board)
                else:
                    batch = self._discover_in_browser(page, role, board, per_board)
            except Exception as exc:
                self.console.print(f"[yellow]{board} search failed: {exc}[/yellow]")
                continue
            for job in batch:
                if job.url in seen:
                    continue
                seen.add(job.url)
                found.append(job)
                if len(found) >= cap:
                    return found
        return found

    def process_job(
        self,
        page: Any,
        job: JobPosting,
        role: ManifestRole,
        applicant: Applicant,
        cv_text: str,
        pdf_path: Path,
        *,
        dry_run: bool,
        auto_submit: bool,
        threshold: int,
    ) -> ApplicationRecord:
        self._applying = True
        try:
            return self._run_one_job(
                page,
                job,
                role,
                applicant,
                cv_text,
                pdf_path,
                dry_run=dry_run,
                auto_submit=auto_submit,
                threshold=threshold,
            )
        except _SkipJob as exc:
            try:
                self._discard_easy_apply(page)
            except Exception:
                pass
            return _record(
                job,
                role,
                None,
                "skipped_manual",
                str(exc) or "Skipped.",
                job.title or "Unknown role",
                job.company or "Unknown",
            )
        except _JobTimeLimit:
            self.console.print("[yellow]Stuck for 5 minutes without the button. Trying the next job.[/yellow]")
            try:
                self._discard_easy_apply(page)
            except Exception:
                pass
            return _record(
                job,
                role,
                None,
                "skipped_manual",
                "Stuck for 5 minutes without the button. Trying the next job.",
                job.title or "Unknown role",
                job.company or "Unknown",
            )
        finally:
            self._applying = False

    def _run_one_job(
        self,
        page: Any,
        job: JobPosting,
        role: ManifestRole,
        applicant: Applicant,
        cv_text: str,
        pdf_path: Path,
        *,
        dry_run: bool,
        auto_submit: bool,
        threshold: int,
    ) -> ApplicationRecord:
        try:
            page.goto(job.url, wait_until="domcontentloaded", timeout=45000)
            self._sleep_short()
            self._pause_if_needed(page)
            title, company, description = _scrape_job_page(page)
            title = title or job.title or "Unknown role"
            company = company or job.company or "Unknown"
            if len(job.description) > len(description):
                description = job.description
            if len(description.strip()) < 80:
                return _record(job, role, None, "error", "Could not read a job description from the page.", title, company)
            fit = self._score(cv_text, role, description)
        except _JobTimeLimit:
            raise
        except _SkipJob:
            raise
        except (LLMError, CVError) as exc:
            return _record(job, role, None, "error", str(exc))
        except Exception as exc:
            return _record(job, role, None, "error", f"Could not open the job: {exc}")

        notes = fit.summary
        if fit.dealbreakers:
            notes = (notes + " Dealbreakers: " + "; ".join(fit.dealbreakers)).strip()
        if fit.score < threshold:
            return _record(job, role, fit.score, "below_threshold", notes[:500], title, company)
        if dry_run:
            return _record(job, role, fit.score, "dry_run", notes[:500], title, company)

        form_page = None
        opened_popup = False
        keep_open = False
        try:
            if _is_linkedin_job(page, job):
                linkedin_result = self._apply_on_linkedin(
                    page,
                    job,
                    role,
                    applicant,
                    cv_text,
                    pdf_path,
                    auto_submit=auto_submit,
                    score=fit.score,
                    notes=notes,
                    title=title,
                    company=company,
                )
                if linkedin_result is not None:
                    return linkedin_result
            form_page, opened_popup = self._open_application(page)
            root = form_page
            if _may_have_greenhouse_form(form_page, job):
                root = _wait_for_application_root(form_page) or form_page
                try:
                    root.locator(".application--questions").first.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
            self._pause_if_needed(form_page)
            uploaded = False
            for _step in range(6):
                self._pause_if_needed(form_page)
                if not uploaded:
                    uploaded = self._upload_cv(root, pdf_path)
                reason = self._fill_step(root, applicant, cv_text, role)
                if reason:
                    action = self._wait(reason, allow_submit=False)
                    if action == "skip":
                        return _record(job, role, fit.score, "skipped_manual", reason[:500], title, company)
                    self._fill_step(root, applicant, cv_text, role)
                if self._click_first(root, NEXT_SELECTORS, _accept_next):
                    self._sleep_short()
                    root.wait_for_timeout(600)
                    continue
                break

            action = self._confirm_submit(
                "The form is filled. Fix any missing fields in the browser, then type submit. Type skip to leave it.",
                auto_submit=auto_submit,
                incomplete=self._unfilled_required(root),
            )
            if action == "skip":
                return _record(job, role, fit.score, "skipped_manual", "Left for you before submit.", title, company)
            if action is not None and action != "submit":
                keep_open = True
                return _record(job, role, fit.score, "pending_review", "Waiting for manual submit.", title, company)

            if self._click_first(root, SUBMIT_SELECTORS, _accept_submit):
                self._sleep_short()
                return _record(job, role, fit.score, "submitted", notes[:500], title, company)
            action = self._wait(
                "The submit button was not found. Submit it yourself, or type skip.",
                allow_submit=True,
                stuck=True,
            )
            if action == "submit" and self._click_first(root, SUBMIT_SELECTORS, _accept_submit):
                return _record(job, role, fit.score, "submitted", notes[:500], title, company)
            if action == "skip":
                return _record(job, role, fit.score, "skipped_manual", "Submit was not found, so this job was skipped.", title, company)
            keep_open = True
            return _record(job, role, fit.score, "pending_review", "Form filled. Submit was not clicked.", title, company)
        except _JobTimeLimit:
            raise
        except _SkipJob:
            raise
        except ConfigError:
            raise
        except Exception as exc:
            return _record(job, role, fit.score, "error", f"Application failed: {exc}", title, company)
        finally:
            if opened_popup and form_page is not None and not keep_open:
                try:
                    form_page.close()
                except Exception:
                    pass

    def _discover_in_browser(self, page: Any, role: ManifestRole, board: str, cap: int) -> list[JobPosting]:
        query = " ".join(role.keywords[:6]) or role.title
        if self.settings.remote_only and "remote" not in query.lower():
            query = f"{query} remote"
        location = role.location or self.settings.job_location
        page.goto(_search_url(board, query, location), wait_until="domcontentloaded", timeout=45000)
        self._sleep_short()
        self._pause_if_needed(page)
        links = _collect_links(page, board, cap)
        if not links and board == "linkedin" and _linkedin_login_wall(page):
            self._wait(
                "LinkedIn needs you to sign in. Log in in the browser window, then press Enter. The session is reused next time.",
                allow_submit=False,
            )
            page.goto(_search_url(board, query, location), wait_until="domcontentloaded", timeout=45000)
            self._sleep_short()
            links = _collect_links(page, board, cap)
        postings = []
        for url in links:
            postings.append(
                JobPosting(title=role.title, company="", url=canonical_url(url), description="", board=board)
            )
        return postings

    def _discover_greenhouse(self, role: ManifestRole, cap: int) -> list[JobPosting]:
        tokens = self.settings.greenhouse_board_tokens()
        if not tokens:
            raise ConfigError("Greenhouse is enabled but GREENHOUSE_BOARDS is empty.")
        postings: list[JobPosting] = []
        for token in tokens:
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
            request = urllib.request.Request(url, headers={"User-Agent": "rolefit-auto-apply/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                self.console.print(f"[yellow]Greenhouse board '{token}' returned HTTP {exc.code}.[/yellow]")
                continue
            except urllib.error.URLError as exc:
                self.console.print(f"[yellow]Greenhouse board '{token}' could not be reached: {exc.reason}[/yellow]")
                continue
            data = json.loads(payload)
            for item in data.get("jobs", []):
                description = html_to_text(html_lib.unescape(item.get("content") or ""))
                title = _clean(item.get("title") or "")
                if not keyword_match(title, description, role.keywords, role.title):
                    continue
                location = ""
                loc = item.get("location") or {}
                if isinstance(loc, dict):
                    location = _clean(loc.get("name") or "")
                absolute = item.get("absolute_url") or ""
                if not absolute:
                    continue
                company = token
                postings.append(
                    JobPosting(
                        title=title or role.title,
                        company=company,
                        url=canonical_url(absolute),
                        description=description[:12000],
                        board="greenhouse",
                    )
                )
                if len(postings) >= cap:
                    return postings
        return postings

    def _score(self, cv_text: str, role: ManifestRole, description: str) -> JobFit:
        user = (
            f"Target role: {role.title}\n\n"
            f"CV:\n{cv_text[:8000]}\n\n"
            f"Job description:\n{description[:8000]}"
        )
        payload = self.llm.complete_json(_SCORE_SYSTEM, user, temperature=0.1, max_tokens=700)
        return JobFit.model_validate(payload)

    def _answer(self, question: str, cv_text: str, applicant: Applicant, options: list[str], limit: int) -> ScreeningAnswer:
        option_block = ""
        if options:
            option_block = "Choose exactly one of these options:\n" + "\n".join(f"- {option}" for option in options) + "\n"
        user = (
            f"Profile:\n{_applicant_block(applicant)}\n\n"
            f"CV:\n{cv_text[:8000]}\n\n"
            f"{option_block}"
            f"Question:\n{question}\n\n"
            f"Keep the answer under {limit} characters."
        )
        payload = self.llm.complete_json(_ANSWER_SYSTEM, user, temperature=0.1, max_tokens=700)
        answer = ScreeningAnswer.model_validate(payload)
        if options and answer.answer and not _option_match(answer.answer, options):
            answer.needs_review = True
            answer.answer = ""
        answer.answer = answer.answer[:limit]
        return answer

    def _open_application(self, page: Any) -> tuple[Any, bool]:
        popup = None
        clicked = False
        try:
            with page.context.expect_page(timeout=2500) as waited:
                clicked = self._click_first(page, APPLY_SELECTORS, _accept_apply)
                if not clicked:
                    raise RuntimeError("no apply control")
            popup = waited.value
        except Exception:
            if not clicked:
                self._click_first(page, APPLY_SELECTORS, _accept_apply)
        if popup is not None:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            self._sleep_short()
            return popup, True
        self._sleep_short()
        return page, False

    def _apply_on_linkedin(
        self,
        page: Any,
        job: JobPosting,
        role: ManifestRole,
        applicant: Applicant,
        cv_text: str,
        pdf_path: Path,
        *,
        auto_submit: bool,
        score: float,
        notes: str,
        title: str,
        company: str,
    ) -> ApplicationRecord | None:
        """Fill LinkedIn Easy Apply. External Apply buttons fall through to the generic form filler."""
        if not self._ensure_linkedin_session(page, job.url):
            return _record(job, role, score, "error", "LinkedIn is still asking you to sign in.", title, company)
        self._dismiss_linkedin_cookies(page)
        self._wait_for_linkedin_apply(page)
        if not self._has_control(page, LINKEDIN_EASY_APPLY_SELECTORS, _accept_easy_apply):
            return None
        if not self._open_easy_apply(page):
            if _linkedin_needs_login(page) and self._ensure_linkedin_session(page, job.url):
                self._open_easy_apply(page)
            if _linkedin_modal(page) is None:
                return _record(
                    job,
                    role,
                    score,
                    "error",
                    "Easy Apply did not open, so this job was left unsubmitted.",
                    title,
                    company,
                )
        self.console.print("[green]Easy Apply opened[/green]")
        uploaded = False
        prompted_for_step = False
        for _step in range(8):
            self._pause_if_needed(page)
            self._click_continue_applying(page)
            modal = _linkedin_modal(page)
            if modal is None:
                if _linkedin_application_sent(page):
                    return _record(job, role, score, "submitted", notes[:500], title, company)
                break
            if not uploaded:
                _reveal_resume_upload(modal)
                uploaded = self._upload_cv(page, pdf_path, modal)
            reason = self._fill_step(page, applicant, cv_text, role, modal)
            try:
                page.evaluate("() => { const el = document.activeElement; if (el && el.blur) el.blur(); }")
            except Exception:
                pass
            outcome = self._advance_easy_apply(page, modal, auto_submit=auto_submit)
            if outcome == "next":
                self.console.print("[dim]LinkedIn application: next step[/dim]")
                prompted_for_step = False
                continue
            if outcome == "submitted":
                return _record(job, role, score, "submitted", notes[:500], title, company)
            if outcome == "pending":
                return _record(job, role, score, "pending_review", "Left the LinkedIn application for you.", title, company)
            if outcome == "skip":
                self._discard_easy_apply(page)
                return _record(job, role, score, "skipped_manual", "Skipped the LinkedIn application.", title, company)
            if outcome == "error" and reason:
                action = self._wait(reason, allow_submit=False)
                if action == "skip":
                    self._discard_easy_apply(page)
                    return _record(job, role, score, "skipped_manual", reason[:500], title, company)
                self._fill_step(page, applicant, cv_text, role, modal)
                prompted_for_step = False
                continue
            if reason and outcome == "":
                action = self._wait(reason, allow_submit=False)
                if action == "skip":
                    self._discard_easy_apply(page)
                    return _record(job, role, score, "skipped_manual", reason[:500], title, company)
                self._fill_step(page, applicant, cv_text, role, modal)
                prompted_for_step = False
                continue
            if not prompted_for_step:
                prompted_for_step = True
                action = self._wait(
                    "LinkedIn's Next button is disabled or missing. Fill the highlighted fields in the browser, then press Enter. Type skip to move on.",
                    allow_submit=False,
                    stuck=True,
                )
                if action == "skip":
                    self._discard_easy_apply(page)
                    return _record(job, role, score, "skipped_manual", "Skipped a LinkedIn step that could not advance.", title, company)
                continue
            break
        if _linkedin_application_sent(page):
            return _record(job, role, score, "submitted", notes[:500], title, company)
        self._discard_easy_apply(page)
        return _record(
            job,
            role,
            score,
            "error",
            "LinkedIn Easy Apply did not reach Submit. This job was left unsubmitted.",
            title,
            company,
        )

    def _ensure_linkedin_session(self, page: Any, job_url: str) -> bool:
        if not _linkedin_needs_login(page):
            return True
        if self._linkedin_login_skipped:
            return False
        action = self._wait(
            "LinkedIn needs you to sign in. Log in in the browser window, then press Enter. The session is reused next time. Type skip to pass over LinkedIn jobs for this run.",
            allow_submit=False,
        )
        if action == "skip":
            self._linkedin_login_skipped = True
            return False
        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
            self._sleep_short()
        except Exception:
            return False
        return not _linkedin_needs_login(page)

    def _dismiss_linkedin_cookies(self, page: Any) -> None:
        self._click_first(
            page,
            [
                "button[action-type='ACCEPT']",
                "button.artdeco-global-alert__action",
            ],
            lambda label: "accept" in label.lower() and "terms" not in label.lower(),
        )

    def _wait_for_linkedin_apply(self, page: Any) -> None:
        try:
            page.locator(
                ".jobs-apply-button--top-card, button.jobs-apply-button, button[aria-label*='Easy Apply'], a[aria-label*='Easy Apply'], a:has-text('Apply')"
            ).first.wait_for(state="visible", timeout=10000)
        except Exception:
            pass

    def _open_easy_apply(self, page: Any) -> bool:
        if not self._click_first(page, LINKEDIN_EASY_APPLY_SELECTORS, _accept_easy_apply):
            return False
        for _ in range(20):
            if _linkedin_modal(page) is not None:
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    pass
                return True
            try:
                page.wait_for_timeout(400)
            except Exception:
                return False
        return False

    def _advance_easy_apply(self, page: Any, modal: Any, *, auto_submit: bool) -> str:
        """Click the dialog's Next or Submit button.

        Returns next, submitted, pending, skip, error, or ''.
        """
        found: tuple[Any, str] | None = None
        for _attempt in range(4):
            if modal is not None:
                found = _last_accepted_button(modal, _LINKEDIN_MODAL_ACTION_SELECTORS)
            if found is None:
                found = _last_accepted_button(page, _LINKEDIN_PAGE_ACTION_SELECTORS)
            if found is not None:
                break
            try:
                page.wait_for_timeout(400)
            except Exception:
                break
        if found is None:
            return ""
        element, kind = found
        label = _control_label(element)
        if kind == "submit":
            incomplete = self._unfilled_required(page, modal)
            if not auto_submit or incomplete:
                action = self._confirm_submit(
                    "The LinkedIn form is filled. Fix any missing fields in the browser, then type submit. Type skip to leave it.",
                    auto_submit=auto_submit,
                    incomplete=incomplete,
                )
                if action == "skip":
                    return "skip"
                if action not in {None, "submit"}:
                    return "pending"
            self.console.print(f"[dim]LinkedIn application: {label or 'Submit'}[/dim]")
            if not _activate(element):
                return ""
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            if _linkedin_application_sent(page) or _linkedin_modal(page) is None:
                return "submitted"
            if _linkedin_form_error(page):
                return "error"
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
            if _linkedin_application_sent(page) or _linkedin_modal(page) is None:
                return "submitted"
            return "error"
        self.console.print(f"[dim]LinkedIn application: {label or 'Next'}[/dim]")
        if not _activate(element):
            return ""
        try:
            page.wait_for_timeout(900)
        except Exception:
            pass
        if _linkedin_form_error(page):
            return "error"
        return "next"

    def _click_linkedin_next(self, page: Any, modal: Any) -> bool:
        """Click Next, Continue, or Review inside the open Easy Apply dialog."""
        if self._click_easy_apply_next_once(page, modal):
            return True
        if not _has_matching_control(modal or page, LINKEDIN_NEXT_SELECTORS, _accept_next, disabled_only=True):
            return False
        for _attempt in range(3):
            try:
                page.wait_for_timeout(400)
            except Exception:
                return False
            if self._click_easy_apply_next_once(page, modal):
                return True
        return False

    def _click_easy_apply_next_once(self, page: Any, modal: Any) -> bool:
        if modal is not None and (
            self._click_role_button(modal, _NEXT_BUTTON_NAME)
            or self._click_first(modal, LINKEDIN_NEXT_SELECTORS, _accept_next)
        ):
            return True
        return self._click_first(page, LINKEDIN_NEXT_PAGE_SELECTORS, _accept_next)

    def _linkedin_submit_ready(self, page: Any, modal: Any) -> bool:
        if modal is not None and self._has_control(modal, LINKEDIN_SUBMIT_SELECTORS, _accept_submit):
            return True
        return self._has_control(page, LINKEDIN_SUBMIT_PAGE_SELECTORS, _accept_submit)

    def _click_linkedin_submit(self, page: Any, modal: Any) -> bool:
        if modal is not None and self._click_first(modal, LINKEDIN_SUBMIT_SELECTORS, _accept_submit):
            return True
        return self._click_first(page, LINKEDIN_SUBMIT_PAGE_SELECTORS, _accept_submit)

    def _click_role_button(self, scope: Any, name: re.Pattern[str]) -> bool:
        try:
            buttons = scope.get_by_role("button", name=name)
            count = min(buttons.count(), 6)
        except Exception:
            return False
        for index in range(count):
            element = buttons.nth(index)
            try:
                if not element.is_visible() or _is_disabled(element):
                    continue
                if not _accept_next(_control_label(element)):
                    continue
                element.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                continue
            if _activate(element):
                return True
        return False

    def _click_continue_applying(self, page: Any) -> None:
        self._click_first(
            page,
            ["button[aria-label='Continue applying']", "button:has-text('Continue applying')"],
            lambda label: "continue applying" in label.lower(),
        )

    def _discard_easy_apply(self, page: Any) -> None:
        if _linkedin_modal(page) is None:
            return
        self._click_first(
            page,
            [
                ".jobs-easy-apply-modal button[aria-label='Dismiss']",
                ".jobs-easy-apply-content button[aria-label='Dismiss']",
                "button[aria-label='Dismiss']",
            ],
            lambda label: "dismiss" in label.lower(),
        )
        try:
            page.wait_for_timeout(400)
        except Exception:
            pass
        self._click_first(
            page,
            ["button[data-control-name='discard_application_confirm_btn']", "button:has-text('Discard')"],
            lambda label: "discard" in label.lower(),
        )

    def _fill_step(
        self,
        page: Any,
        applicant: Applicant,
        cv_text: str,
        role: ManifestRole,
        root: Any = None,
    ) -> str:
        applicant = _with_role_facts(applicant, _role_facts(role, self.settings))
        _fill_name_ids(root or page, applicant)
        fields = _extract_fields(root or page, page)
        radios: dict[str, list[dict[str, Any]]] = {}
        others: list[dict[str, Any]] = []
        for field in fields:
            if field["type"] == "input:radio":
                radios.setdefault(field["name"], []).append(field)
            else:
                others.append(field)

        unresolved: list[str] = []
        llm_calls = 0
        self._identify_form_tags(radios, others)
        self._recover_missing_controls(root or page, page, radios, others, applicant)
        mark_phone_prefix_before_phone(others)
        for name, group in radios.items():
            question = group[0]["group_label"] or group[0]["label"] or name
            labels = [item["choice"] or item["label"] for item in group]
            required = any(item["required"] for item in group)
            if _already_checked(group):
                continue
            tag = group[0].get("tag")
            if tag in {"skip", "resume"}:
                continue
            if tag:
                choice = self._choice_for_tag(tag, question, labels, applicant, cv_text, required, unresolved)
                if choice:
                    _check_matching(group, choice)
                continue
            choice = self._choose_option(question, labels, applicant, cv_text, required, unresolved)
            if choice:
                _check_matching(group, choice)

        for field in others:
            label = field["label"]
            required = field["required"]
            field_type = field["type"]
            if field_type in {"input:hidden", "input:submit", "input:button", "input:search", "input:file", "input:password"}:
                if field_type == "input:password":
                    unresolved.append("Password field: sign in yourself")
                continue
            if _has_value(field):
                continue
            tag = field.get("tag")
            if tag:
                llm_calls = self._fill_tagged(field, tag, applicant, cv_text, unresolved, llm_calls)
                continue
            if re.search(r"\bfollow\b", label, re.IGNORECASE) and not required:
                continue
            if is_eeo_question(label):
                if required:
                    unresolved.append(f"Voluntary question left blank: {label}")
                continue
            if is_sensitive_question(label):
                unresolved.append(f"Sensitive field left for you: {label}")
                continue
            if is_agreement_question(label):
                if required:
                    unresolved.append(f"Confirm this agreement yourself: {label}")
                continue
            if field_type.startswith("input:checkbox"):
                country = (_country_from_location(applicant.location) or "").lower()
                choice = (field.get("choice") or label).strip(" *").lower()
                if country and (choice == country or choice.startswith(country)):
                    _write(field, "yes")
                elif required and ("?" in label or len(label) > 40):
                    unresolved.append(label)
                continue
            if is_salary_question(label):
                if applicant.salary_expectation and field_type != "select":
                    _write(field, applicant.salary_expectation)
                elif required:
                    unresolved.append(f"Salary was not found in the CV: {label}")
                continue
            if is_work_auth_question(label):
                if field_type == "select":
                    choice = yes_no_answer(label, applicant.work_authorization)
                    options = field["options"]
                    if choice and _select_choice(field, choice):
                        continue
                    if required:
                        unresolved.append(f"Work authorization needs a manual answer: {label}")
                    continue
                if applicant.work_authorization and not field_type.startswith("input:checkbox"):
                    _write(field, applicant.work_authorization)
                    continue
                if required:
                    unresolved.append(f"Work authorization was not found in the CV: {label}")
                continue

            profile_value = profile_value_for_label(label, applicant)
            if profile_value and field_type != "select":
                if field_type.startswith("input:checkbox"):
                    continue
                if not _write(field, profile_value) and required:
                    unresolved.append(label)
                continue
            if field_type == "select" and profile_value and _write_profile_value(field, profile_value):
                continue

            question_like = "?" in label or len(label) > 24 or field_type == "textarea"
            if not required and not question_like:
                continue
            if llm_calls >= 8:
                if required:
                    unresolved.append(label)
                continue
            options = field["options"] if field_type == "select" else []
            limit = 1500 if re.search(r"cover letter|why (do you|are you)", label, re.IGNORECASE) else 500
            try:
                answer = self._answer(label, cv_text, applicant, options, limit)
            except LLMError as exc:
                unresolved.append(f"{label} ({exc})")
                continue
            llm_calls += 1
            if answer.needs_review or not answer.answer:
                if required:
                    unresolved.append(label)
                continue
            if field_type == "select":
                if not _select_choice(field, answer.answer) and required:
                    unresolved.append(label)
            elif field_type.startswith("input:checkbox"):
                if answer.answer.strip().lower() in {"yes", "true", "y"}:
                    _write(field, "yes")
                elif required:
                    unresolved.append(label)
            else:
                _write(field, answer.answer)

        filled_labels = self._infer_empty_fields(radios, others, applicant, cv_text)
        unresolved = _without_filled(unresolved, filled_labels)
        caught_up, still_missing = self._fill_blocks_still_empty(root or page, page, applicant, cv_text)
        unresolved = _without_filled(unresolved, caught_up)
        unresolved.extend(still_missing)

        unique: list[str] = []
        for item in unresolved:
            if item not in unique:
                unique.append(item)
        if not unique:
            return ""
        preview = "; ".join(unique[:3])
        return "These fields need you in the browser: " + preview

    def _infer_empty_fields(
        self,
        radios: dict[str, list[dict[str, Any]]],
        others: list[dict[str, Any]],
        applicant: Applicant,
        cv_text: str,
    ) -> set[str]:
        """Fill controls the profile tags did not cover, using the profile and CV."""
        requests = infer_field_requests(radios, others)
        if not requests:
            return set()
        self.console.print(f"[dim]Inferring {len(requests)} empty fields from the profile[/dim]")
        try:
            answers = run_profile_infer_agent(
                self.llm,
                requests,
                _applicant_block(applicant),
                cv_text,
            )
        except LLMError as exc:
            self.console.print(f"[yellow]Could not infer the empty fields ({exc}).[/yellow]")
            return set()
        filled = apply_inferred_answers(radios, requests, answers)
        if filled:
            self.console.print(f"[dim]Filled {len(filled)} fields from the profile[/dim]")
        return filled

    def _fill_blocks_still_empty(
        self,
        scope: Any,
        page: Any,
        applicant: Applicant,
        cv_text: str,
    ) -> tuple[set[str], list[str]]:
        """Fill required fields that were still empty after the first pass."""
        blocks = _missing_block_fields(scope, page)
        if not blocks:
            return set(), []
        self.console.print(f"[dim]Completing {len(blocks)} fields still empty[/dim]")
        filled: set[str] = set()
        llm_calls = 0
        for field in blocks:
            label = str(field.get("label") or "")
            if _has_value(field):
                filled.add(label)
                continue
            if _leave_blank(label):
                continue
            profile_value = profile_value_for_label(label, applicant)
            tag = label_profile_tag(label)
            if not profile_value and tag:
                profile_value = value_for_tag(tag, applicant)
            if tag == "work_authorization" and field["type"] == "select":
                choice = yes_no_answer(label, applicant.work_authorization)
                if choice and _select_choice(field, choice):
                    filled.add(label)
                    self.console.print(f"[dim]Filled {label}[/dim]")
                    continue
            if profile_value and field["type"] == "select" and _write_profile_value(field, profile_value):
                filled.add(label)
                self.console.print(f"[dim]Filled {label}[/dim]")
                continue
            if profile_value and field["type"] != "select" and _write(field, profile_value):
                filled.add(label)
                self.console.print(f"[dim]Filled {label}[/dim]")
                continue
            if not field.get("required"):
                continue
            if llm_calls >= 8:
                continue
            options = field.get("options") or []
            if field["type"] == "role:listbox" and not options:
                options = _read_listbox_options(page, field["handle"])
                field["options"] = options
            try:
                answer = self._answer(label, cv_text, applicant, options, 500)
            except LLMError as exc:
                self.console.print(f"[yellow]Could not answer {label} ({exc}).[/yellow]")
                continue
            llm_calls += 1
            if answer.needs_review or not answer.answer:
                continue
            wrote = _select_choice(field, answer.answer) if field["type"] == "select" else _write(field, answer.answer)
            if not wrote and options:
                wrote = _click_suggestion(page, answer.answer)
            if wrote or _has_value(field):
                filled.add(label)
                self.console.print(f"[dim]Filled {label}[/dim]")
        still: list[str] = []
        for field in blocks:
            label = str(field.get("label") or "")
            if not field.get("required") or label in filled:
                continue
            try:
                empty = not _has_value(field)
            except Exception:
                empty = True
            if empty and label not in still:
                still.append(label)
        return filled, still

    def _identify_form_tags(self, radios: dict[str, list[dict[str, Any]]], others: list[dict[str, Any]]) -> None:
        """Run the form agent. A failed run leaves controls unlabeled for label matching."""
        requests = form_tag_requests(radios, others)
        if not requests:
            return
        try:
            tags = run_form_tag_agent(self.llm, requests)
        except LLMError as exc:
            self.console.print(f"[yellow]The form agent failed ({exc}). Matching labels directly.[/yellow]")
            return
        if not tags:
            self.console.print("[yellow]The form agent did not label this form. Matching labels directly.[/yellow]")
            return
        assign_form_tags(radios, others, tags, requests)
        self.console.print(f"[dim]Form agent labeled {len(tags)} fields[/dim]")

    def _recover_missing_controls(
        self,
        scope: Any,
        page: Any,
        radios: dict[str, list[dict[str, Any]]],
        others: list[dict[str, Any]],
        applicant: Applicant,
    ) -> None:
        """When name, email, or phone is not on the controls already found, ask an agent to pick them."""
        known = [item for group in radios.values() for item in group] + others
        missing = missing_contact_tags(found_contact_tags(known), applicant)
        if not any(tag in _CORE_CONTACT_TAGS for tag in missing):
            return
        extras = _extract_extra_controls(scope, page)
        requests = recovery_requests(radios, others, extras)
        if not requests:
            return
        listed = ", ".join(missing)
        self.console.print(f"[dim]Looking for missing fields: {listed}[/dim]")
        try:
            tags = run_control_recovery_agent(self.llm, requests, missing)
        except LLMError as exc:
            self.console.print(f"[yellow]Could not find the missing fields ({exc}).[/yellow]")
            return
        if not tags:
            self.console.print("[yellow]The agent could not find those fields on the other controls.[/yellow]")
            return
        for row in requests:
            tag = tags.get(row["id"])
            if not tag:
                continue
            field = row["field"]
            field["tag"] = tag
            if row["kind"] == "extra":
                others.append(field)
        self.console.print(f"[dim]Found {len(tags)} missing fields on other controls[/dim]")

    def _choice_for_tag(
        self,
        tag: str,
        question: str,
        options: list[str],
        applicant: Applicant,
        cv_text: str,
        required: bool,
        unresolved: list[str],
    ) -> str | None:
        if tag == "eeo":
            if required:
                unresolved.append(f"Voluntary question left blank: {question}")
            return None
        if tag == "sensitive":
            unresolved.append(f"Sensitive field left for you: {question}")
            return None
        if tag == "agreement":
            if required:
                unresolved.append(f"Confirm this agreement yourself: {question}")
            return None
        if tag == "work_authorization":
            choice = yes_no_answer(question, applicant.work_authorization)
            matched = _option_match(choice, options) if choice else None
            if matched:
                return matched
            if required:
                unresolved.append(f"Work authorization needs a manual answer: {question}")
            return None
        if tag in _PROFILE_TAGS:
            value = value_for_tag(tag, applicant)
            matched = _option_match(value, options) if value else None
            if matched:
                return matched
            if required:
                unresolved.append(question)
            return None
        if tag in {"screening", "cover_letter"}:
            try:
                answer = self._answer(question, cv_text, applicant, options, 80)
            except LLMError:
                if required:
                    unresolved.append(question)
                return None
            if answer.needs_review or not answer.answer:
                if required:
                    unresolved.append(question)
                return None
            return _option_match(answer.answer, options)
        return None

    def _fill_tagged(
        self,
        field: dict[str, Any],
        tag: str,
        applicant: Applicant,
        cv_text: str,
        unresolved: list[str],
        llm_calls: int,
    ) -> int:
        """Type into one control using the tag from the live form. Returns the LLM call count."""
        label = field["label"]
        required = field["required"]
        field_type = field["type"]
        question = field.get("group_label") or label

        if tag in {"skip", "resume"}:
            return llm_calls
        if tag == "eeo":
            if required:
                unresolved.append(f"Voluntary question left blank: {label}")
            return llm_calls
        if tag == "sensitive":
            unresolved.append(f"Sensitive field left for you: {label}")
            return llm_calls
        if tag == "agreement":
            if required:
                unresolved.append(f"Confirm this agreement yourself: {label}")
            return llm_calls
        if tag != "phone_prefix" and _write_profile_value(field, profile_value_for_label(label, applicant)):
            return llm_calls
        if tag == "salary":
            if field_type == "select" and applicant.salary_expectation and _select_choice(field, applicant.salary_expectation):
                return llm_calls
            if applicant.salary_expectation and field_type != "select":
                _write(field, applicant.salary_expectation)
            elif required:
                unresolved.append(f"Salary was not found in the CV: {label}")
            return llm_calls
        if tag == "work_authorization":
            if field_type == "select" or field_type.startswith("input:checkbox"):
                choice = yes_no_answer(question, applicant.work_authorization)
                if field_type == "select" and choice and _select_choice(field, choice):
                    return llm_calls
                if field_type.startswith("input:checkbox") and choice == "Yes":
                    _write(field, "yes")
                    return llm_calls
                if required:
                    unresolved.append(f"Work authorization needs a manual answer: {label}")
                return llm_calls
            if applicant.work_authorization:
                _write(field, applicant.work_authorization)
                return llm_calls
            if required:
                unresolved.append(f"Work authorization was not found in the CV: {label}")
            return llm_calls
        if tag == "phone_prefix" and field_type == "select":
            country = _country_from_location(applicant.location) or ""
            if applicant.phone_prefix and _select_phone_prefix(field, applicant.phone_prefix, country):
                return llm_calls
            if required:
                unresolved.append(label)
            return llm_calls
        if tag in _PROFILE_TAGS:
            value = value_for_tag(tag, applicant)
            if field_type == "select" or str(field_type).startswith("role:"):
                if value and _write_profile_value(field, value):
                    return llm_calls
                if required:
                    unresolved.append(label)
                return llm_calls
            if field_type.startswith("input:checkbox"):
                if tag == "country":
                    country = (_country_from_location(applicant.location) or "").lower()
                    choice = (field.get("choice") or label).strip(" *").lower()
                    if country and (choice == country or choice.startswith(country)):
                        _write(field, "yes")
                return llm_calls
            if value:
                if not _write(field, value) and required:
                    unresolved.append(label)
                return llm_calls
            if required:
                unresolved.append(label)
            return llm_calls
        if tag in {"screening", "cover_letter"}:
            return self._answer_and_write(
                field,
                applicant,
                cv_text,
                unresolved,
                llm_calls,
                cover=tag == "cover_letter",
            )
        return llm_calls

    def _answer_and_write(
        self,
        field: dict[str, Any],
        applicant: Applicant,
        cv_text: str,
        unresolved: list[str],
        llm_calls: int,
        *,
        cover: bool,
    ) -> int:
        label = field["label"]
        required = field["required"]
        field_type = field["type"]
        question_like = cover or "?" in label or len(label) > 24 or field_type == "textarea"
        if not required and not question_like:
            return llm_calls
        if llm_calls >= 8:
            if required:
                unresolved.append(label)
            return llm_calls
        options = field["options"] if field_type == "select" else []
        limit = 1500 if cover or re.search(r"cover letter|why (do you|are you)", label, re.IGNORECASE) else 500
        try:
            answer = self._answer(label, cv_text, applicant, options, limit)
        except LLMError as exc:
            unresolved.append(f"{label} ({exc})")
            return llm_calls
        llm_calls += 1
        if answer.needs_review or not answer.answer:
            if required:
                unresolved.append(label)
            return llm_calls
        if field_type == "select":
            if not _select_choice(field, answer.answer) and required:
                unresolved.append(label)
        elif field_type.startswith("input:checkbox"):
            if answer.answer.strip().lower() in {"yes", "true", "y"}:
                _write(field, "yes")
            elif required:
                unresolved.append(label)
        else:
            _write(field, answer.answer)
        return llm_calls

    def _choose_option(
        self,
        question: str,
        options: list[str],
        applicant: Applicant,
        cv_text: str,
        required: bool,
        unresolved: list[str],
    ) -> str | None:
        if is_eeo_question(question):
            if required:
                unresolved.append(f"Voluntary question left blank: {question}")
            return None
        if is_work_auth_question(question):
            choice = yes_no_answer(question, applicant.work_authorization)
            if choice and _option_match(choice, options):
                return _option_match(choice, options)
            if required:
                unresolved.append(f"Work authorization needs a manual answer: {question}")
            return None
        profile_value = profile_value_for_label(question, applicant)
        if profile_value:
            matched = _option_match(profile_value, options)
            if matched:
                return matched
        if not required and "?" not in question:
            return None
        try:
            answer = self._answer(question, cv_text, applicant, options, 80)
        except LLMError:
            if required:
                unresolved.append(question)
            return None
        if answer.needs_review or not answer.answer:
            if required:
                unresolved.append(question)
            return None
        return _option_match(answer.answer, options)

    def _upload_cv(self, page: Any, pdf_path: Path, root: Any = None) -> bool:
        scope = _questions_scope(root or page)
        try:
            inputs = scope.locator("input[type='file']")
            count = inputs.count()
        except Exception:
            return False
        uploaded = False
        for index in range(count):
            handle = inputs.nth(index)
            try:
                label = _label_for(page, handle).lower()
                name = (handle.get_attribute("name") or "").lower()
                field_id = (handle.get_attribute("id") or "").lower()
                blob = f"{label} {name} {field_id}"
                if "cover" in blob and "resume" not in blob and "cv" not in blob:
                    continue
                handle.set_input_files(str(pdf_path))
                uploaded = True
            except Exception:
                continue
        if uploaded:
            self.console.print(f"[green]Uploaded[/green] {pdf_path.name}")
        return uploaded

    def _has_control(self, page: Any, selectors: list[str], accept) -> bool:
        return _matching_control(page, selectors, accept) is not None

    def _click_first(self, page: Any, selectors: list[str], accept) -> bool:
        for element in _iter_controls(page, selectors, accept):
            try:
                element.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            if _activate(element):
                return True
        return False

    def _confirm_submit(self, message: str, *, auto_submit: bool, incomplete: bool) -> str | None:
        """Wait before Submit so a person can fix empty fields.

        Returns None only for an unattended ``--auto-submit`` run whose required
        fields are already filled. That run clicks Submit without a prompt.
        """
        if sys.stdin.isatty() or not auto_submit or incomplete:
            return self._wait(message, allow_submit=True)
        return None

    def _pause_if_needed(self, page: Any) -> None:
        reason = _page_needs_human(page)
        if not reason:
            return
        if self.headless:
            reason += " Re-run without --headless so the browser is visible."
        self._wait_out_human_check(page, reason)

    def _wait_out_human_check(self, page: Any, reason: str) -> None:
        """Wait until the person finishes the check. Clicking the box is enough.

        The run continues on its own once the challenge leaves the page.
        A bot click on the checkbox does not count, so this does not click it.
        """
        if not sys.stdin.isatty():
            raise ConfigError("A human check is on the page. Re-run in an interactive terminal and complete it in the browser.")
        progress = self._progress
        if progress is not None:
            progress.stop()
        try:
            self.console.print(
                Panel(
                    reason + " Click it in the browser. This continues on its own once the check is gone. Type skip to leave the job.",
                    title="Action needed",
                    border_style="yellow",
                )
            )
            deadline = time.monotonic() + JOB_LIMIT_SECONDS
            while time.monotonic() < deadline:
                if _page_needs_human(page) is None:
                    self.console.print("[green]Human check cleared. Continuing the application.[/green]")
                    try:
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
                    return
                answer = _read_stdin(1.0)
                if answer is None:
                    continue
                if answer.strip().lower() == "skip":
                    if self._applying:
                        raise _SkipJob("Skipped while a human check was on the page.")
                    self.console.print("[dim]The check is still on the page. Finish it in the browser.[/dim]")
                    continue
                self.console.print("[dim]The check is still on the page. Finish it in the browser.[/dim]")
            if self._applying:
                raise _JobTimeLimit()
        finally:
            if progress is not None:
                progress.start()

    def _wait(self, reason: str, allow_submit: bool, *, stuck: bool = False) -> str:
        progress = self._progress
        if progress is not None:
            progress.stop()
        try:
            self.console.print(Panel(reason, title="Action needed", border_style="yellow"))
            if not sys.stdin.isatty():
                if stuck:
                    raise _JobTimeLimit()
                raise ConfigError("This step needs a person at the terminal. Re-run in an interactive terminal.")
            hint = "Press Enter to continue, or type skip: "
            if allow_submit:
                hint = "Type submit to send, skip to leave it, or Enter to keep reviewing: "
            if not stuck:
                return self.console.input(hint).strip().lower()
            self.console.print(f"{hint}[dim]{JOB_LIMIT_SECONDS}s before this stuck step is skipped.[/dim]")
            answer = _read_stdin(JOB_LIMIT_SECONDS)
            if answer is None:
                raise _JobTimeLimit()
            return answer.strip().lower()
        finally:
            if progress is not None:
                progress.start()

    def _unfilled_required(self, page: Any, root: Any = None) -> bool:
        for field in _extract_fields(root or page, page):
            if field["required"] and not _has_value(field) and field["type"] not in {"input:hidden", "input:file"}:
                return True
        return False

    def _sleep_short(self) -> None:
        low = min(self.settings.min_delay_seconds, self.settings.max_delay_seconds)
        high = max(self.settings.min_delay_seconds, self.settings.max_delay_seconds)
        time.sleep(random.uniform(low, high))

    def _sleep_between(self) -> None:
        low = min(self.settings.min_between_applications, self.settings.max_between_applications)
        high = max(self.settings.min_between_applications, self.settings.max_between_applications)
        time.sleep(random.uniform(low, high))


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigError("Playwright is not installed. Run: pip install -r requirements.txt && playwright install chromium") from exc
    return sync_playwright


def _search_url(board: str, query: str, location: str) -> str:
    q = quote_plus(query)
    loc = quote_plus(location)
    if board == "linkedin":
        return f"https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}"
    if board == "indeed":
        return f"https://www.indeed.com/jobs?q={q}&l={loc}"
    raise ConfigError(f"No browser search URL for board '{board}'.")


def _collect_links(page: Any, board: str, cap: int) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for _ in range(5):
        for selector in LINK_SELECTORS.get(board, []):
            try:
                hrefs = page.eval_on_selector_all(selector, "els => els.map(el => el.href).filter(Boolean)")
            except Exception:
                continue
            for href in hrefs:
                if any(bad in href for bad in ("/pagead/", "doubleclick", "googleads")):
                    continue
                url = canonical_url(href)
                if board == "linkedin" and "/jobs/view/" not in url:
                    continue
                if board == "indeed" and "jk=" not in url and "viewjob" not in url and "/rc/clk" not in url and "/viewjob" not in href:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                found.append(url)
                if len(found) >= cap:
                    return found
        try:
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(700)
        except Exception:
            break
    return found


def _accept_browser_dialog(dialog: Any) -> None:
    try:
        dialog.accept()
    except Exception:
        pass


def _is_linkedin_job(page: Any, job: JobPosting) -> bool:
    if job.board == "linkedin":
        return True
    return "linkedin.com" in f"{getattr(page, 'url', '')} {job.url}".lower()


def _last_accepted_button(scope: Any, selectors: list[str]) -> tuple[Any, str] | None:
    """The last enabled Next or Submit button in the first selector that has one.

    Easy Apply puts Back on the left and Next or Submit on the right, so the
    last matching footer button is the one that advances the dialog.
    """
    for selector in selectors:
        found: tuple[Any, str] | None = None
        try:
            loc = scope.locator(selector)
            count = min(loc.count(), 12)
        except Exception:
            continue
        for index in range(count):
            element = loc.nth(index)
            try:
                if not element.is_visible() or _is_disabled(element):
                    continue
                label = _control_label(element)
            except Exception:
                continue
            if _pagination_button(element, label):
                continue
            if _accept_submit(label):
                found = (element, "submit")
            elif _accept_next(label):
                found = (element, "next")
        if found is not None:
            return found
    return None


def _pagination_button(element: Any, label: str) -> bool:
    if "page" in label.lower():
        return True
    try:
        class_name = element.get_attribute("class") or ""
    except Exception:
        return False
    return "pagination" in class_name or "jobs-search" in class_name


def _activate(element: Any) -> bool:
    for kwargs in ({"timeout": 2500}, {"timeout": 1500, "force": True}):
        try:
            element.click(**kwargs)
            return True
        except Exception:
            continue
    try:
        element.evaluate("el => el.click()")
        return True
    except Exception:
        return False


def _has_matching_control(scope: Any, selectors: list[str], accept, *, disabled_only: bool = False) -> bool:
    for selector in selectors:
        try:
            loc = scope.locator(selector)
            count = min(loc.count(), 15)
        except Exception:
            continue
        for index in range(count):
            element = loc.nth(index)
            try:
                if not element.is_visible():
                    continue
                if not accept(_control_label(element)):
                    continue
                if disabled_only and not _is_disabled(element):
                    continue
                return True
            except Exception:
                continue
    return False


def _iter_controls(page: Any, selectors: list[str], accept, limit: int = 15):
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = min(loc.count(), limit)
        except Exception:
            continue
        for index in range(count):
            element = loc.nth(index)
            try:
                if not element.is_visible() or _is_disabled(element):
                    continue
                if not accept(_control_label(element)):
                    continue
            except Exception:
                continue
            yield element


def _matching_control(page: Any, selectors: list[str], accept):
    return next(_iter_controls(page, selectors, accept), None)


def _linkedin_modal(page: Any):
    found = None
    for selector in LINKEDIN_MODAL_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                found = loc
                break
        except Exception:
            continue
    if found is None:
        for selector in ("dialog", "[role='dialog']"):
            try:
                loc = page.locator(selector)
                count = min(loc.count(), 5)
            except Exception:
                continue
            for index in range(count):
                item = loc.nth(index)
                try:
                    if item.is_visible() and _dialog_is_application(item):
                        found = item
                        break
                except Exception:
                    continue
            if found is not None:
                break
    if found is None:
        return None
    try:
        dialog = found.locator("xpath=ancestor-or-self::*[self::dialog or @role='dialog'][1]")
        if dialog.count() > 0 and dialog.first.is_visible():
            return dialog.first
    except Exception:
        pass
    return found


def _dialog_is_application(dialog: Any) -> bool:
    try:
        buttons = dialog.locator("button")
        count = min(buttons.count(), 8)
        has_fields = dialog.locator("input:not([type='hidden']), textarea, select").count() > 0
    except Exception:
        return False
    if count < 1:
        return False
    has_next = False
    for index in range(count):
        try:
            label = _control_label(buttons.nth(index))
        except Exception:
            continue
        if _accept_submit(label):
            return True
        if _accept_next(label):
            has_next = True
    return has_next and has_fields


def _linkedin_step_marker(modal: Any) -> str:
    """A fingerprint of the current Easy Apply step, used to see whether Next moved."""
    if modal is None:
        return ""
    for selector, attr in (("progress", "value"), ("[role='progressbar']", "aria-valuenow")):
        try:
            loc = modal.locator(selector).first
            if loc.count() > 0:
                value = _clean(loc.get_attribute(attr) or "")
                if value:
                    return f"{attr}:{value}"
        except Exception:
            continue
    try:
        text = modal.inner_text(timeout=1000)
    except Exception:
        text = ""
    match = re.search(r"step\s+\d+\s+of\s+\d+", text, flags=re.IGNORECASE)
    if match:
        return match.group(0).lower()
    parts: list[str] = []
    try:
        buttons = modal.locator("footer button, .jobs-easy-apply-footer button, button.artdeco-button--primary, button")
        count = min(buttons.count(), 8)
    except Exception:
        count = 0
    for index in range(count):
        try:
            label = _control_label(buttons.nth(index))
        except Exception:
            continue
        if _accept_next(label) or _accept_submit(label):
            parts.append(label.lower())
            break
    try:
        labels = modal.locator("label, legend, h2, h3")
        count = min(labels.count(), 12)
    except Exception:
        count = 0
    for index in range(count):
        handle = labels.nth(index)
        try:
            if not handle.is_visible():
                continue
            label = _clean(handle.inner_text(timeout=300))
        except Exception:
            continue
        if not label or label.lower().startswith("apply to"):
            continue
        if label not in parts:
            parts.append(label)
        if len(parts) >= 6:
            break
    try:
        fields = modal.locator("input:not([type='hidden']), textarea, select")
        count = min(fields.count(), 8)
    except Exception:
        count = 0
    for index in range(count):
        handle = fields.nth(index)
        try:
            if not handle.is_visible():
                continue
            blob = _clean(
                " ".join(
                    part
                    for part in (
                        handle.get_attribute("aria-label") or "",
                        handle.get_attribute("name") or "",
                        handle.get_attribute("placeholder") or "",
                    )
                    if part
                )
            )
        except Exception:
            continue
        if blob and blob not in parts:
            parts.append(blob)
        if len(parts) >= 8:
            break
    return "|".join(parts)


def _linkedin_step_moved(page: Any, before: str) -> bool:
    if _linkedin_application_sent(page):
        return True
    modal = _linkedin_modal(page)
    if modal is None:
        return True
    if before and _linkedin_step_marker(modal) != before:
        return True
    if _linkedin_form_error(page):
        return False
    return not before


def _linkedin_needs_login(page: Any) -> bool:
    url = (getattr(page, "url", "") or "").lower()
    if any(part in url for part in ("/login", "/uas/login", "/checkpoint/", "/authwall", "signin")):
        return True
    try:
        field = page.locator("input[type='password']").first
        if field.count() > 0 and field.is_visible():
            return True
    except Exception:
        pass
    return _linkedin_login_wall(page)


def _linkedin_application_sent(page: Any) -> bool:
    try:
        text = page.inner_text("body", timeout=2000).lower()
    except Exception:
        text = ""
    if any(
        marker in text
        for marker in (
            "application was sent",
            "application has been sent",
            "application sent",
            "application submitted",
            "application was submitted",
        )
    ):
        return True
    for selector in (
        "button[aria-label^='Applied']",
        ".jobs-apply-button:has-text('Applied')",
        ".jobs-easy-apply-modal button:has-text('Done')",
    ):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _linkedin_form_error(page: Any) -> bool:
    modal = _linkedin_modal(page)
    if modal is None:
        return False
    try:
        err = modal.locator(".artdeco-inline-feedback--error")
        return err.count() > 0 and err.first.is_visible()
    except Exception:
        return False


def _reveal_resume_upload(modal: Any) -> None:
    try:
        toggle = modal.locator("label:has-text('Upload resume'), span:has-text('Upload resume')").first
        if toggle.count() > 0 and toggle.is_visible():
            toggle.click(timeout=1500)
    except Exception:
        pass


def _linkedin_login_wall(page: Any) -> bool:
    if "linkedin.com" not in (page.url or ""):
        return False
    try:
        text = page.inner_text("body", timeout=1500).lower()
    except Exception:
        return False
    return "sign in" in text and ("join now" in text or "email or phone" in text)


def _jobs_from_file(path: Path, role: ManifestRole) -> list[JobPosting]:
    if not path.exists():
        raise ConfigError(f"URL file not found: {path}")
    jobs: list[JobPosting] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        url = canonical_url(raw)
        if url in seen:
            continue
        seen.add(url)
        jobs.append(JobPosting(title=role.title, company="", url=url, description="", board="urls"))
    return jobs


@dataclass
class _RoleFacts:
    city: str = ""
    country: str = ""
    school: str = ""
    degree: str = ""


def _role_facts(role: ManifestRole, settings: Settings) -> _RoleFacts:
    """City, country, and the first education entry on the tailored CV for this role."""
    if not role.metadata_path:
        return _RoleFacts()
    path = settings.resolve(role.metadata_path)
    if not path.is_file():
        return _RoleFacts()
    try:
        cv = TailoredCV.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return _RoleFacts()
    education = cv.education[0] if cv.education else None
    return _RoleFacts(
        city=(cv.city or "").strip(),
        country=(cv.country or "").strip(),
        school=(education.school or "").strip() if education else "",
        degree=(education.degree or "").strip() if education else "",
    )


def _with_role_facts(applicant: Applicant, facts: _RoleFacts) -> Applicant:
    """Use the tailored CV's city, country, school, and degree on the form."""
    updates: dict[str, str] = {}
    if facts.school:
        updates["school"] = facts.school
    if facts.degree:
        updates["degree"] = facts.degree
    if facts.city or facts.country:
        updates["location"] = ", ".join(part for part in (facts.city, facts.country) if part)
    if not updates:
        return applicant
    return applicant.model_copy(update=updates)


def _school_from_role(role: ManifestRole, settings: Settings) -> str:
    """The first school on the tailored CV JSON for the role being applied."""
    return _role_facts(role, settings).school


def _role_context(role: ManifestRole, settings: Settings) -> str:
    metadata = settings.resolve(role.metadata_path) if role.metadata_path else None
    if metadata and metadata.exists():
        try:
            return TailoredCV.model_validate_json(metadata.read_text(encoding="utf-8")).as_text()
        except Exception:
            pass
    pdf = settings.resolve(role.pdf_path)
    return extract_text(pdf)


def _scrape_job_page(page: Any) -> tuple[str, str, str]:
    title = ""
    try:
        heading = page.locator("h1").first
        if heading.count() > 0 and heading.is_visible():
            title = _clean(heading.inner_text(timeout=800))
    except Exception:
        title = ""
    if not title:
        try:
            title = _clean(page.title())
        except Exception:
            title = ""

    company = ""
    for selector in COMPANY_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            text = _clean(loc.inner_text(timeout=800))
            if text:
                company = text
                break
        except Exception:
            continue

    best = ""
    for selector in DESCRIPTION_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            text = loc.inner_text(timeout=1000)
        except Exception:
            continue
        cleaned = (text or "").strip()
        if len(cleaned) > len(best):
            best = cleaned
    if len(best) < 80:
        try:
            body = page.inner_text("body", timeout=2000)
        except Exception:
            body = ""
        if len(body or "") > len(best):
            best = body or ""
    return title, company[:160], best.strip()[:12000]


def _page_needs_human(page: Any) -> str | None:
    try:
        text = page.inner_text("body", timeout=1500)
    except Exception:
        text = ""
    reason = needs_human(page.url or "", text or "")
    if reason:
        return reason
    for selector in ("iframe[src*='challenges.cloudflare.com']", "iframe[src*='hcaptcha']", "iframe[src*='recaptcha']", "iframe[title*='human' i]", "iframe[title*='captcha' i]", "#captcha"):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return "A CAPTCHA is visible. Complete it in the browser, then continue."
        except Exception:
            continue
    return None


def _may_have_greenhouse_form(page: Any, job: JobPosting) -> bool:
    blob = f"{job.board} {job.url} {getattr(page, 'url', '')}".lower()
    return "greenhouse" in blob or "gh_jid" in blob


def _wait_for_application_root(page: Any, timeout_s: float = 12.0):
    """Return the page or frame that contains Greenhouse's question form."""
    deadline = time.monotonic() + timeout_s
    while True:
        for scope in _iter_scopes(page):
            if _scope_has_questions(scope):
                return scope
        if time.monotonic() >= deadline:
            return None
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)


def _iter_scopes(page: Any):
    yield page
    frames = getattr(page, "frames", None)
    if not frames:
        return
    main = getattr(page, "main_frame", None)
    for frame in frames:
        if frame is page or frame is main:
            continue
        yield frame


def _scope_has_questions(scope: Any) -> bool:
    try:
        loc = scope.locator(".application--questions").first
        return loc.count() > 0 and loc.is_visible()
    except Exception:
        return False


def _fill_name_ids(scope: Any, applicant: Applicant) -> None:
    """Type the profile name into inputs whose id is full_name, first_name, or last_name."""
    values = {
        "first_name": (applicant.first_name or "").strip(),
        "last_name": (applicant.last_name or "").strip(),
        "full_name": (applicant.full_name or "").strip(),
    }
    containers: list[Any] = []
    try:
        form = _questions_scope(scope)
    except Exception:
        form = scope
    containers.append(form)
    if scope is not form:
        containers.append(scope)
    for field_id, value in values.items():
        if not value:
            continue
        for container in containers:
            if _write_id_field(container, field_id, value):
                break


def _write_id_field(scope: Any, field_id: str, value: str) -> bool:
    try:
        loc = scope.locator(f'[id="{field_id}"]')
        count = loc.count()
    except Exception:
        return False
    for index in range(count):
        handle = loc.nth(index)
        try:
            if not handle.is_visible():
                continue
            tag = (handle.evaluate("el => el.tagName") or "").lower()
            if tag not in {"input", "textarea"}:
                nested = handle.locator("input, textarea")
                if nested.count() == 0:
                    continue
                handle = nested.first
            current = ""
            try:
                current = _clean(handle.input_value(timeout=400))
            except Exception:
                current = ""
            if current == _clean(value):
                return True
            if _write({"handle": handle, "type": "input:text"}, value):
                return True
        except Exception:
            continue
    return False


def _questions_scope(scope: Any):
    """Search the application form, including fields that sit above the question block.

    Greenhouse puts First Name, Email, Phone, and School outside `.application--questions`.
    The question block's parent form contains both.
    """
    try:
        questions = scope.locator(".application--questions")
        if questions.count() > 0:
            form = questions.first.locator("xpath=ancestor::form[1]")
            if form.count() > 0:
                return form.first
            return questions.first
    except Exception:
        pass
    return scope


def _visible_field_ancestor(handle: Any) -> bool:
    """True when a hidden native select still belongs to a visible Greenhouse field."""
    try:
        box = handle.locator("xpath=ancestor::*[contains(@class,'field')][1]")
        return box.count() > 0 and box.first.is_visible()
    except Exception:
        return False


def _missing_block_fields(scope: Any, page: Any | None = None) -> list[dict[str, Any]]:
    """Required controls that are still empty, including ones the first selectors missed."""
    document = page or getattr(scope, "page", scope)
    scope = _questions_scope(scope)
    try:
        blocks = scope.locator(".field")
        count = blocks.count()
        if count == 0:
            blocks = scope.locator("fieldset")
            count = blocks.count()
    except Exception:
        return []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(min(count, 40)):
        block = blocks.nth(index)
        try:
            block.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass
        field = _field_from_block(block, document, index)
        if field is None:
            continue
        key = str(field.get("name") or field.get("label") or "")
        if not key or key in seen:
            continue
        try:
            if _has_value(field):
                continue
        except Exception:
            continue
        if not field.get("required") and not label_profile_tag(str(field.get("label") or "")):
            continue
        seen.add(key)
        fields.append(field)
    return fields


def _field_from_block(block: Any, document: Any, index: int) -> dict[str, Any] | None:
    raw = ""
    try:
        label_node = block.locator("label").first
        if label_node.count() > 0:
            raw = _clean(label_node.inner_text(timeout=400))
    except Exception:
        raw = ""
    label = _clean(raw.replace("*", ""))
    handle, field_type = _block_control(block)
    if handle is None:
        return None
    if not label:
        label = _label_for(document, handle) or f"Field {index + 1}"
    required = "*" in raw or _is_required(handle)
    if not required:
        try:
            legend = block.locator("legend").first
            if legend.count() > 0 and "*" in _clean(legend.inner_text(timeout=300)):
                required = True
        except Exception:
            pass
    options = _select_options(handle) if field_type == "select" else []
    try:
        name = handle.get_attribute("name") or handle.get_attribute("id") or label or f"block_{index}"
    except Exception:
        name = label or f"block_{index}"
    return {
        "handle": handle,
        "label": label,
        "group_label": "",
        "context": "",
        "choice": "",
        "name": name,
        "required": required,
        "type": field_type,
        "options": options,
    }


def _block_control(block: Any) -> tuple[Any, str] | tuple[None, str]:
    try:
        visible = block.locator(
            "textarea, input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio']):not([type='submit']):not([type='button'])"
        )
        for index in range(min(visible.count(), 6)):
            candidate = visible.nth(index)
            if not candidate.is_visible():
                continue
            tag = (candidate.evaluate("el => el.tagName") or "").lower()
            role = (candidate.get_attribute("role") or "").lower()
            if role in {"combobox", "textbox"}:
                return candidate, f"role:{role}"
            if tag == "textarea":
                return candidate, "textarea"
            input_type = (candidate.get_attribute("type") or "text").lower()
            return candidate, f"input:{input_type}"
    except Exception:
        pass
    try:
        selects = block.locator("select")
        if selects.count() > 0:
            return selects.first, "select"
    except Exception:
        pass
    try:
        button = block.locator("button[aria-haspopup='listbox'], [role='combobox']").first
        if button.count() > 0 and button.is_visible():
            return button, "role:listbox"
    except Exception:
        pass
    return None, ""


def _read_listbox_options(page: Any, handle: Any) -> list[str]:
    try:
        handle.click(timeout=1000)
        page.wait_for_timeout(300)
    except Exception:
        return []
    options: list[str] = []
    try:
        loc = page.locator("[role='option'], .select__option")
        for index in range(min(loc.count(), 12)):
            item = loc.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = _clean(item.inner_text(timeout=300))
            except Exception:
                continue
            if text and text not in options:
                options.append(text)
    except Exception:
        options = []
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return options


def _click_suggestion(page: Any, desired: str) -> bool:
    """Choose an open dropdown option that matches the answer."""
    for selector in ("[role='option']", ".select__option"):
        try:
            loc = page.locator(selector)
            count = min(loc.count(), 15)
        except Exception:
            continue
        visible: list[tuple[Any, str]] = []
        for index in range(count):
            item = loc.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = _clean(item.inner_text(timeout=300))
            except Exception:
                continue
            if text:
                visible.append((item, text))
        if not visible:
            continue
        choice = _option_match(desired, [text for _, text in visible])
        if not choice:
            continue
        for item, text in visible:
            if text == choice:
                try:
                    item.click(timeout=1000)
                    return True
                except Exception:
                    return False
    return False


def _extract_fields(scope: Any, page: Any | None = None) -> list[dict[str, Any]]:
    document = page or getattr(scope, "page", scope)
    scope = _questions_scope(scope)
    fields: list[dict[str, Any]] = []
    try:
        loc = scope.locator("input:not([type='hidden']), textarea, select")
        count = loc.count()
    except Exception:
        return fields
    for index in range(count):
        handle = loc.nth(index)
        try:
            tag = (handle.evaluate("el => el.tagName") or "").lower()
            hidden_select = tag == "select" and not handle.is_visible() and _visible_field_ancestor(handle)
            if not handle.is_visible() and not hidden_select:
                continue
            if str(handle.get_attribute("aria-hidden") or "").lower() == "true" and not hidden_select:
                continue
            in_chrome = handle.evaluate("el => !!el.closest('header, nav, footer')")
            if in_chrome:
                continue
            input_type = (handle.get_attribute("type") or "text").lower() if tag == "input" else tag
            field_type = tag if tag in {"textarea", "select"} else f"input:{input_type}"
            label = _label_for(document, handle)
            group_label = _group_label(handle)
            context = _nearby_text(handle)
            choice = ""
            if field_type in {"input:radio", "input:checkbox"}:
                choice = _choice_label(document, handle) or label
            options = _select_options(handle) if field_type == "select" else []
            fields.append(
                {
                    "handle": handle,
                    "label": label or group_label or _humanize_control_name(handle.get_attribute("name") or handle.get_attribute("id") or "") or f"Field {index + 1}",
                    "group_label": group_label,
                    "context": context,
                    "choice": choice,
                    "name": handle.get_attribute("name") or handle.get_attribute("id") or f"field_{index}",
                    "required": _is_required(handle),
                    "type": field_type,
                    "options": options,
                }
            )
        except Exception:
            continue
    return fields


def _extract_extra_controls(scope: Any, page: Any | None = None) -> list[dict[str, Any]]:
    """Visible widgets that are not a plain input, textarea, or select."""
    document = page or getattr(scope, "page", scope)
    scope = _questions_scope(scope)
    fields: list[dict[str, Any]] = []
    try:
        loc = scope.locator(_EXTRA_CONTROL_SELECTOR)
        count = loc.count()
    except Exception:
        return fields
    for index in range(min(count, 30)):
        handle = loc.nth(index)
        try:
            if not handle.is_visible():
                continue
            tag = (handle.evaluate("el => el.tagName") or "").lower()
            if tag in {"input", "textarea", "select"}:
                continue
            role = (handle.get_attribute("role") or "").lower()
            editable = (handle.get_attribute("contenteditable") or "").lower() == "true"
            field_type = "contenteditable" if editable and not role else f"role:{role or 'text'}"
            label = _label_for(document, handle)
            group_label = _group_label(handle)
            fields.append(
                {
                    "handle": handle,
                    "label": label or group_label or f"Control {index + 1}",
                    "group_label": group_label,
                    "context": _nearby_text(handle),
                    "choice": "",
                    "name": handle.get_attribute("id") or handle.get_attribute("name") or f"control_{index}",
                    "required": _is_required(handle),
                    "type": field_type,
                    "options": [],
                }
            )
        except Exception:
            continue
    return fields


def _label_for(page: Any, handle: Any) -> str:
    try:
        element_id = handle.get_attribute("id")
        if element_id:
            safe = element_id.replace("\\", "\\\\").replace("'", "\\'")
            label = page.locator(f"label[for='{safe}']").first
            if label.count() > 0:
                text = _clean(label.inner_text(timeout=400))
                if text:
                    return text
    except Exception:
        pass
    try:
        parent = handle.locator("xpath=ancestor::label[1]").first
        if parent.count() > 0:
            text = _clean(parent.inner_text(timeout=400))
            if text:
                return text
    except Exception:
        pass
    for attr in ("aria-label", "placeholder", "name"):
        try:
            value = handle.get_attribute(attr)
        except Exception:
            value = None
        if value and _clean(value):
            return _clean(value)
    return ""


def _nearby_text(handle: Any) -> str:
    """Text of the control's field wrapper, so the model sees the real question."""
    try:
        text = handle.evaluate(
            """el => {
                const wrap = el.closest(
                    'fieldset, .field, .form-group, .application-question, .fb-form-element, li, [class*="question"], [class*="field"]'
                ) || el.parentElement;
                if (!wrap) return '';
                return (wrap.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 280);
            }"""
        )
    except Exception:
        return ""
    return _clean(text or "")[:280]


def _group_label(handle: Any) -> str:
    try:
        legend = handle.locator("xpath=ancestor::fieldset[1]/legend").first
        if legend.count() > 0:
            return _clean(legend.inner_text(timeout=400))
    except Exception:
        pass
    try:
        labelledby = handle.get_attribute("aria-labelledby")
        if labelledby:
            text = handle.evaluate(
                """(el, id) => {
                    const node = el.ownerDocument.getElementById(id);
                    return node ? node.innerText : "";
                }""",
                labelledby.split()[0],
            )
            return _clean(text or "")
    except Exception:
        pass
    return ""


def _choice_label(page: Any, handle: Any) -> str:
    own = _label_for(page, handle)
    return own


def _select_options(handle: Any) -> list[str]:
    options: list[str] = []
    try:
        loc = handle.locator("option")
        for index in range(loc.count()):
            text = _clean(loc.nth(index).inner_text(timeout=300))
            if not text or text.lower() in {"select", "select...", "select…", "choose", "please select", "--"}:
                continue
            options.append(text)
    except Exception:
        return options
    return options


def _is_required(handle: Any) -> bool:
    try:
        if handle.get_attribute("required") is not None:
            return True
        return str(handle.get_attribute("aria-required")).lower() == "true"
    except Exception:
        return False


def _has_value(field: dict[str, Any]) -> bool:
    handle = field["handle"]
    field_type = field["type"]
    try:
        if field_type == "input:checkbox" or field_type == "input:radio":
            return bool(handle.is_checked())
        if field_type == "select":
            selected = handle.evaluate("el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''")
            text = _clean(selected or "")
            return bool(text) and text.lower() not in {"select", "select...", "choose", "please select"}
        value = handle.input_value(timeout=400)
        return bool(_clean(value))
    except Exception:
        return False


def _already_checked(group: list[dict[str, Any]]) -> bool:
    return any(_has_value(item) for item in group)


def _write(field: dict[str, Any], value: str) -> bool:
    handle = field["handle"]
    field_type = field["type"]
    try:
        if field_type in {"input:checkbox", "input:radio"}:
            if value.strip().lower() in {"yes", "true", "y", "1"}:
                handle.check(timeout=1000)
                return True
            return False
        maxlength = handle.get_attribute("maxlength")
        text = value
        if maxlength and str(maxlength).isdigit():
            text = text[: int(maxlength)]
        role = (handle.get_attribute("role") or "").lower()
        if role in {"combobox", "textbox"} or field_type.startswith("role:"):
            try:
                handle.click(timeout=1000)
            except Exception:
                pass
            try:
                handle.fill(text, timeout=1500)
                handle.press("Enter")
            except Exception:
                pass
            if _value_stuck(handle, text):
                return True
            try:
                suggestion_page = handle.page
            except Exception:
                suggestion_page = None
            if suggestion_page is not None and _click_suggestion(suggestion_page, text):
                return True
            return _value_stuck(handle, text)
        if field_type == "select":
            return _select_choice(field, value)
        if field_type == "contenteditable":
            handle.click(timeout=1000)
        try:
            handle.fill(text, timeout=1500)
        except Exception:
            pass
        if _value_stuck(handle, text):
            return True
        handle.click(timeout=1000)
        _set_native_value(handle, text)
        return _value_stuck(handle, text)
    except Exception:
        return False


def _value_stuck(handle: Any, text: str) -> bool:
    try:
        current = _clean(handle.input_value(timeout=400))
    except Exception:
        return False
    wanted = _clean(text)
    return bool(current) and (wanted in current or current in wanted)


def _set_native_value(handle: Any, text: str) -> None:
    """Set a React-controlled input when Playwright's fill does not stick."""
    handle.evaluate(
        """(el, value) => {
            const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value');
            if (setter && setter.set) setter.set.call(el, value);
            else el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        text,
    )


def _phone_prefix_option(options: list[str], prefix: str, country: str = "") -> str | None:
    """The visible choice for a calling code, preferring +57 over a bare country name."""
    digits = prefix.strip().lstrip("+")
    if digits:
        for option in options:
            compact = option.replace(" ", "")
            if f"+{digits}" in compact or re.search(rf"(?:^|\D){re.escape(digits)}(?:\D|$)", option):
                return option
    if country:
        return _option_match(country, options)
    return None


def _select_phone_prefix(field: dict[str, Any], prefix: str, country: str = "") -> bool:
    """Pick the dropdown option for this calling code, such as +57 or Colombia."""
    chosen = _phone_prefix_option(list(field.get("options") or []), prefix, country)
    if not chosen:
        return False
    return _select_choice(field, chosen)


def _select_choice(field: dict[str, Any], desired: str) -> bool:
    matched = _option_match(desired, field.get("options") or [])
    if not matched:
        return False
    handle = field["handle"]
    try:
        handle.select_option(label=matched)
        return True
    except Exception:
        try:
            handle.select_option(value=matched)
            return True
        except Exception:
            return False


def _check_matching(group: list[dict[str, Any]], choice: str) -> bool:
    target = choice.strip().lower()
    for item in group:
        label = (item.get("choice") or item.get("label") or "").strip().lower()
        if label == target or target in label or label in target:
            return _write(item, "yes")
    return False


def _fold(value: str) -> str:
    """Lowercase and drop accents so Bogotá matches Bogota."""
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold().strip()


def _option_match(desired: str, options: list[str]) -> str | None:
    wanted = _fold(desired)
    if not wanted:
        return None
    for option in options:
        if _fold(option) == wanted:
            return option
    for option in options:
        text = _fold(option)
        if wanted in {"yes", "no", "true", "false"} and text.startswith(wanted):
            return option
    for option in options:
        text = _fold(option)
        if len(wanted) > 3 and (wanted in text or text in wanted):
            return option
    if wanted.startswith("+") and wanted[1:].isdigit():
        for option in options:
            if wanted in _fold(option).replace(" ", ""):
                return option
    return None


def _degree_option(degree: str, options: list[str]) -> str | None:
    """Map ``B.S. in Systems Engineering`` onto a menu choice such as Bachelor's Degree."""
    direct = _option_match(degree, options)
    if direct:
        return direct
    folded = _fold(degree)
    if re.search(r"\b(ph\.?d|doctor|doctorate)\b", folded):
        level = "doctor"
    elif re.search(r"\b(m\.?s\.?|m\.?a\.?|mba|master)\b", folded):
        level = "master"
    elif re.search(r"\b(b\.?s\.?|b\.?a\.?|bachelor)\b", folded):
        level = "bachelor"
    elif "associate" in folded:
        level = "associate"
    else:
        return None
    needles = {
        "doctor": ("doctor", "phd", "ph.d"),
        "master": ("master", "mba", "m.s"),
        "bachelor": ("bachelor", "b.s"),
        "associate": ("associate",),
    }[level]
    for option in options:
        text = _fold(option)
        if any(needle in text for needle in needles):
            return option
    return None


def _write_profile_value(field: dict[str, Any], value: str | None) -> bool:
    """Type a known profile value, picking a degree menu choice when the wording differs."""
    if not value:
        return False
    field_type = str(field.get("type") or "")
    if field_type.startswith("input:checkbox") or field_type.startswith("input:radio"):
        return False
    label = _label_text(str(field.get("label") or ""))
    if field_type == "select":
        if _select_choice(field, value):
            return True
        if _looks_like_degree_field(label):
            choice = _degree_option(value, list(field.get("options") or []))
            if choice and _select_choice(field, choice):
                return True
        return False
    return _write(field, value)


def _applicant_block(applicant: Applicant) -> str:
    lines = [
        f"Name: {applicant.full_name}",
        f"Email: {applicant.email}",
        f"Phone: {applicant.phone}",
        f"Phone prefix: {applicant.phone_prefix}",
        f"Phone number: {applicant.phone_number}",
        f"LinkedIn: {applicant.linkedin_url}",
        f"Portfolio: {applicant.portfolio_url}",
        f"Location: {applicant.location}",
        f"City: {_city_from_location(applicant.location) or ''}",
        f"Country: {_country_from_location(applicant.location) or ''}",
        f"School: {applicant.school}",
        f"Degree: {applicant.degree}",
        f"Work authorization: {applicant.work_authorization}",
        f"Salary expectation: {applicant.salary_expectation}",
    ]
    return "\n".join(line for line in lines if not line.endswith(": "))


def _record(
    job: JobPosting,
    role: ManifestRole,
    score: float | None,
    status: str,
    notes: str,
    title: str | None = None,
    company: str | None = None,
) -> ApplicationRecord:
    return ApplicationRecord(
        job_title=title or job.title or role.title,
        company=company or job.company or "Unknown",
        url=job.url,
        cv_used=role.pdf_path,
        match_score=score,
        timestamp=utc_now(),
        status=status,
        notes=notes,
    )

