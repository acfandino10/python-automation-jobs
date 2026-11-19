"""Form-label mapping and human-check detection shared by board adapters.

These helpers answer only from the candidate profile. They leave demographic
questions, agreements, and anything they cannot ground for a person to review.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from rolefit.models import Applicant, ScreeningAnswer

GENERIC_ROLE_WORDS = frozenset(
    {
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
)

_HUMAN_TEXT = (
    "verify you are human",
    "are you a robot",
    "unusual traffic",
    "security check",
    "complete the captcha",
    "please verify",
    "recaptcha",
    "hcaptcha",
    "g-recaptcha",
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

_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "trackingid",
        "trk",
        "refid",
        "position",
        "pagenum",
        "from",
    }
)


class _HTMLText(HTMLParser):
    """Collect visible text nodes from an HTML fragment."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    """Collapse an HTML fragment to a single line of visible text."""
    parser = _HTMLText()
    parser.feed(value or "")
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def canonical_url(url: str) -> str:
    """Drop tracking parameters while keeping the job id.

    Indeed's ``jk`` parameter is preserved. Other known campaign parameters are removed.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    query = parse_qs(parsed.query, keep_blank_values=False)
    if "jk" in query:
        kept = urlencode({"jk": query["jk"][0]})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", kept, ""))
    kept_pairs: list[tuple[str, str]] = []
    for key, values in query.items():
        if key.lower() in _TRACKING_PARAMS:
            continue
        for value in values:
            kept_pairs.append((key, value))
    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode(kept_pairs), ""))


def keyword_match(title: str, description: str, keywords: list[str], role_title: str) -> bool:
    """Return whether a posting shares a real skill or a distinctive title word.

    Generic words such as ``engineer`` do not count. A keyword must be at least
    three characters and appear in the title or description.
    """
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
    """Return a pause reason when the page is a CAPTCHA or a sign-in wall.

    Args:
        url: Current page URL.
        text: Visible page text.

    Returns:
        A sentence for the operator, or ``None`` when the page looks like an application.
    """
    blob = f"{url}\n{text[:2500]}".lower()
    if any(marker in blob for marker in _HUMAN_TEXT):
        return (
            "The site is showing a human verification check. "
            "Complete it in the browser, then press Enter."
        )
    lowered = url.lower()
    if any(
        part in lowered for part in ("/login", "/uas/login", "signin", "sign-in", "/checkpoint/")
    ):
        return (
            "Sign in is required. Log in in the browser window. "
            "This profile is saved for the next run."
        )
    return None


def is_eeo_question(label: str) -> bool:
    """Return whether a label is a voluntary demographic question."""
    return (
        re.search(
            r"\b(gender|race|ethnic|ethnicity|veteran|disability|pronouns?|"
            r"sexual orientation|self-identify)\b",
            label,
            re.IGNORECASE,
        )
        is not None
    )


def is_sensitive_question(label: str) -> bool:
    """Return whether a label asks for a secret or a government identifier."""
    return (
        re.search(
            r"social security|\bssn\b|credit card|\bcvv\b|\bpassword\b|"
            r"date of birth|driver'?s license|\bnational id\b",
            label,
            re.IGNORECASE,
        )
        is not None
    )


def is_salary_question(label: str) -> bool:
    """Return whether a label asks for pay expectations."""
    return (
        re.search(
            r"\b(salary|compensation|pay expectation|desired pay|expected pay)\b",
            label,
            re.IGNORECASE,
        )
        is not None
    )


def is_agreement_question(label: str) -> bool:
    """Return whether a label is a terms or privacy acknowledgement."""
    return (
        re.search(
            r"\b(i agree|terms of service|privacy policy|terms and conditions|consent to)\b",
            label,
            re.IGNORECASE,
        )
        is not None
    )


def is_work_auth_question(label: str) -> bool:
    """Return whether a label asks about work authorization or sponsorship."""
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
    """Map a configured work-authorization sentence onto Yes or No.

    Returns ``None`` when the sentence does not mention the country in the
    question, or when sponsorship intent is ambiguous. The caller must leave
    those questions for a person.
    """
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


def profile_value_for_label(label: str, applicant: Applicant) -> str | None:
    """Return the profile value that matches a form label, or ``None``.

    Args:
        label: Visible label text.
        applicant: Candidate contact record.
    """
    text = " ".join(label.lower().split())
    if not text:
        return None
    if "given name" in text or (re.search(r"\bfirst\b", text) and "name" in text):
        return applicant.first_name or None
    if any(token in text for token in ("surname", "family name")) or (
        re.search(r"\blast\b", text) and "name" in text
    ):
        return applicant.last_name or None
    if "email" in text or "e-mail" in text:
        return applicant.email or None
    if "phone" in text or "mobile" in text:
        return applicant.phone or None
    if "linkedin" in text:
        return applicant.linkedin_url or None
    if any(token in text for token in ("portfolio", "website", "github")) or (
        re.search(r"\burl\b", text) and "job" not in text
    ):
        return applicant.portfolio_url or None
    if re.search(r"\b(city|location)\b", text) and "authorization" not in text:
        return applicant.location or None
    if text in {"name", "name *"} or re.search(r"\b(full name|your name)\b", text):
        return applicant.full_name or None
    if is_salary_question(text):
        return applicant.salary_expectation or None
    return None


def answer_from_profile(question: str, applicant: Applicant) -> ScreeningAnswer | None:
    """Answer a question from the profile, or return ``None`` when a model may try.

    Demographic, agreement, and sensitive questions are marked for review and
    must not be sent to a model.
    """
    if (
        is_eeo_question(question)
        or is_agreement_question(question)
        or is_sensitive_question(question)
    ):
        return ScreeningAnswer(answer="", needs_review=True)
    direct = profile_value_for_label(question, applicant)
    if direct:
        return ScreeningAnswer(answer=direct, needs_review=False)
    if is_work_auth_question(question):
        mapped = yes_no_answer(question, applicant.work_authorization)
        if mapped is None:
            return ScreeningAnswer(answer="", needs_review=True)
        return ScreeningAnswer(answer=mapped, needs_review=False)
    if is_salary_question(question):
        if applicant.salary_expectation:
            return ScreeningAnswer(answer=applicant.salary_expectation, needs_review=False)
        return ScreeningAnswer(answer="", needs_review=True)
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
