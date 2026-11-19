"""Search job boards and fill applications with the CV generated for that role.

The runner uses a persistent Playwright profile so the user can log in once.
It pauses for sign-in, CAPTCHA, and any required field it cannot answer from
the profile or the CV. It does not solve CAPTCHAs or invent work authorization.
"""

from __future__ import annotations

import html as html_lib
import json
import random
import re
import sys
import time
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
    ".jobs-unified-top-card__company-name",
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

_HUMAN_TEXT = (
    "verify you are human",
    "are you a robot",
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
Use only facts in the profile and CV. Do not invent employers, dates, metrics, or visa status.
If the answer is not in the material, set needs_review to true and answer to "".
Plain text only, no markdown.
Return JSON only.
"""


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


def profile_value_for_label(label: str, applicant: Applicant) -> str | None:
    text = " ".join(label.lower().split())
    if not text:
        return None
    if "given name" in text or (re.search(r"\bfirst\b", text) and "name" in text):
        return applicant.first_name or None
    if any(token in text for token in ("surname", "family name")) or (re.search(r"\blast\b", text) and "name" in text):
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


def _accept_next(label: str) -> bool:
    text = label.lower().strip()
    if any(bad in text for bad in ("job", "page", "result", "search")):
        return False
    return text in {"continue", "next", "review", "save and continue"} or text.startswith("continue")


def _accept_submit(label: str) -> bool:
    text = label.lower().strip()
    if "search" in text or not text:
        return False
    return "submit" in text or "send application" in text


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
            eligible = 0
            try:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    console=self.console,
                ) as progress:
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
                context.close()
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
            form_page, opened_popup = self._open_application(page)
            self._pause_if_needed(form_page)
            uploaded = False
            for _step in range(6):
                self._pause_if_needed(form_page)
                if not uploaded:
                    uploaded = self._upload_cv(form_page, pdf_path)
                reason = self._fill_step(form_page, applicant, cv_text, role)
                if reason:
                    action = self._wait(reason, allow_submit=False)
                    if action == "skip":
                        return _record(job, role, fit.score, "skipped_manual", reason[:500], title, company)
                    self._fill_step(form_page, applicant, cv_text, role)
                if self._click_first(form_page, NEXT_SELECTORS, _accept_next):
                    self._sleep_short()
                    form_page.wait_for_timeout(600)
                    continue
                break

            if not auto_submit:
                action = self._wait(
                    "Review the filled form in the browser. Type submit to send it, or skip to leave it.",
                    allow_submit=True,
                )
                if action != "submit":
                    keep_open = True
                    return _record(job, role, fit.score, "pending_review", "Waiting for manual submit.", title, company)
            elif self._unfilled_required(form_page):
                action = self._wait(
                    "Some required fields are still empty. Fill them, type submit, or skip.",
                    allow_submit=True,
                )
                if action == "skip":
                    return _record(job, role, fit.score, "skipped_manual", "Required fields left empty.", title, company)
                if action != "submit":
                    keep_open = True
                    return _record(job, role, fit.score, "pending_review", "Auto-submit stopped on an incomplete form.", title, company)

            if self._click_first(form_page, SUBMIT_SELECTORS, _accept_submit):
                self._sleep_short()
                return _record(job, role, fit.score, "submitted", notes[:500], title, company)
            action = self._wait("The submit button was not found. Submit it yourself, or type skip.", allow_submit=True)
            if action == "submit" and self._click_first(form_page, SUBMIT_SELECTORS, _accept_submit):
                return _record(job, role, fit.score, "submitted", notes[:500], title, company)
            keep_open = True
            return _record(job, role, fit.score, "pending_review", "Form filled. Submit was not clicked.", title, company)
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

    def _fill_step(self, page: Any, applicant: Applicant, cv_text: str, role: ManifestRole) -> str:
        fields = _extract_fields(page)
        radios: dict[str, list[dict[str, Any]]] = {}
        others: list[dict[str, Any]] = []
        for field in fields:
            if field["type"] == "input:radio":
                radios.setdefault(field["name"], []).append(field)
            else:
                others.append(field)

        unresolved: list[str] = []
        llm_calls = 0
        for name, group in radios.items():
            question = group[0]["group_label"] or group[0]["label"] or name
            labels = [item["choice"] or item["label"] for item in group]
            required = any(item["required"] for item in group)
            if _already_checked(group):
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
                _write(field, profile_value)
                continue
            if field_type == "select" and profile_value and _select_choice(field, profile_value):
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

        unique: list[str] = []
        for item in unresolved:
            if item not in unique:
                unique.append(item)
        if not unique:
            return ""
        preview = "; ".join(unique[:3])
        return "These fields need you in the browser: " + preview

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

    def _upload_cv(self, page: Any, pdf_path: Path) -> bool:
        try:
            inputs = page.locator("input[type='file']")
            count = inputs.count()
        except Exception:
            return False
        uploaded = False
        for index in range(count):
            handle = inputs.nth(index)
            try:
                label = _label_for(page, handle).lower()
                name = (handle.get_attribute("name") or "").lower()
                blob = f"{label} {name}"
                if "cover" in blob and "resume" not in blob and "cv" not in blob:
                    continue
                handle.set_input_files(str(pdf_path))
                uploaded = True
            except Exception:
                continue
        if uploaded:
            self.console.print(f"[green]Uploaded[/green] {pdf_path.name}")
        return uploaded

    def _click_first(self, page: Any, selectors: list[str], accept) -> bool:
        for selector in selectors:
            try:
                loc = page.locator(selector)
                count = min(loc.count(), 6)
            except Exception:
                continue
            for index in range(count):
                element = loc.nth(index)
                try:
                    if not element.is_visible():
                        continue
                    label = _clean(element.get_attribute("value") or "") or _clean(element.inner_text(timeout=500))
                    if not accept(label):
                        continue
                    element.click(timeout=2000)
                    return True
                except Exception:
                    continue
        return False

    def _pause_if_needed(self, page: Any) -> None:
        reason = _page_needs_human(page)
        if reason:
            if self.headless:
                reason += " Re-run without --headless so the browser is visible."
            self._wait(reason, allow_submit=False)

    def _wait(self, reason: str, allow_submit: bool) -> str:
        self.console.print(Panel(reason, title="Action needed", border_style="yellow"))
        if not sys.stdin.isatty():
            raise ConfigError("This step needs a person at the terminal. Re-run in an interactive terminal.")
        hint = "Press Enter to continue, or type skip: "
        if allow_submit:
            hint = "Type submit to send, skip to leave it, or Enter to keep reviewing: "
        answer = self.console.input(hint).strip().lower()
        return answer

    def _unfilled_required(self, page: Any) -> bool:
        for field in _extract_fields(page):
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
    for selector in ("iframe[src*='hcaptcha']", "iframe[title*='captcha' i]", "#captcha"):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return "A CAPTCHA is visible. Complete it in the browser, then continue."
        except Exception:
            continue
    return None


def _extract_fields(page: Any) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    try:
        loc = page.locator("input:not([type='hidden']), textarea, select")
        count = loc.count()
    except Exception:
        return fields
    for index in range(count):
        handle = loc.nth(index)
        try:
            if not handle.is_visible():
                continue
            in_chrome = handle.evaluate("el => !!el.closest('header, nav, footer')")
            if in_chrome:
                continue
            tag = (handle.evaluate("el => el.tagName") or "").lower()
            input_type = (handle.get_attribute("type") or "text").lower() if tag == "input" else tag
            field_type = tag if tag in {"textarea", "select"} else f"input:{input_type}"
            label = _label_for(page, handle)
            group_label = _group_label(handle)
            choice = ""
            if field_type == "input:radio":
                choice = _choice_label(page, handle) or label
            options = _select_options(handle) if field_type == "select" else []
            fields.append(
                {
                    "handle": handle,
                    "label": label or group_label or f"Field {index + 1}",
                    "group_label": group_label,
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
        if field_type == "select":
            return _select_choice(field, value)
        maxlength = handle.get_attribute("maxlength")
        text = value
        if maxlength and maxlength.isdigit():
            text = text[: int(maxlength)]
        handle.fill(text, timeout=1500)
        return True
    except Exception:
        return False


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


def _option_match(desired: str, options: list[str]) -> str | None:
    wanted = desired.strip().lower()
    if not wanted:
        return None
    for option in options:
        if option.strip().lower() == wanted:
            return option
    for option in options:
        text = option.strip().lower()
        if wanted in {"yes", "no", "true", "false"} and text.startswith(wanted):
            return option
    for option in options:
        text = option.strip().lower()
        if len(wanted) > 3 and (wanted in text or text in wanted):
            return option
    return None


def _applicant_block(applicant: Applicant) -> str:
    lines = [
        f"Name: {applicant.full_name}",
        f"Email: {applicant.email}",
        f"Phone: {applicant.phone}",
        f"LinkedIn: {applicant.linkedin_url}",
        f"Portfolio: {applicant.portfolio_url}",
        f"Location: {applicant.location}",
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

