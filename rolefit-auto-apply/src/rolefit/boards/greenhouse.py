"""Greenhouse adapter.

Listing uses the public Job Board API (``boards-api.greenhouse.io``), one request
per board token. The application form is filled in a persistent Playwright
profile so a person can sign in once and can complete a CAPTCHA by hand.

The browser session is injectable. Tests pass a fake session and never launch
Chromium.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from rolefit.boards.base import ApplyContext, ApplyResult, BaseJobBoard, SearchQuery, register_board
from rolefit.exceptions import BoardError
from rolefit.forms import html_to_text, keyword_match, needs_human
from rolefit.models import JobListing

_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
_PROFILE_LABELS = (
    "First Name",
    "Last Name",
    "Email",
    "Phone",
    "LinkedIn Profile",
    "Website",
    "Location",
)


class PageSession(Protocol):
    """The browser operations a board adapter is allowed to use.

    A Playwright implementation and a test double both satisfy this protocol.
    Adapters should not reach past it into raw Playwright handles.
    """

    def open(self, url: str) -> None:
        """Navigate to a job URL."""

    def body_text(self) -> str:
        """Return the visible text of the current page."""

    def current_url(self) -> str:
        """Return the current page URL."""

    def fill_labeled(self, label: str, value: str) -> bool:
        """Fill the control associated with ``label``. Return whether it was found."""

    def upload_resume(self, path: Path) -> bool:
        """Attach a résumé file. Return whether a file input was found."""

    def question_labels(self) -> list[str]:
        """Return visible question labels that still need an answer."""

    def answer_text_question(self, label: str, value: str) -> bool:
        """Type ``value`` into the question matching ``label``."""

    def click_submit(self) -> bool:
        """Click the submit control. Return whether it was found."""

    def pause(self, reason: str) -> None:
        """Block so a person can use the open browser window."""

    def close(self) -> None:
        """Release the browser. Safe to call more than once."""


class PlaywrightPageSession:
    """Persistent Chromium session used by the Greenhouse adapter.

    The profile directory keeps cookies between runs. Install the browser extra
    and Chromium before constructing this class:

    ``pip install -e '.[browser]' && playwright install chromium``
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        pause: Callable[[str], None],
    ) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._pause = pause
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    def open(self, url: str) -> None:
        """Open ``url`` in the persistent profile."""
        self._ensure()
        self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    def body_text(self) -> str:
        """Return the body text, or an empty string when the page has no body."""
        self._ensure()
        try:
            return str(self._page.inner_text("body"))
        except Exception:
            return ""

    def current_url(self) -> str:
        """Return the page URL."""
        self._ensure()
        return str(self._page.url)

    def fill_labeled(self, label: str, value: str) -> bool:
        """Fill the first field whose accessible name contains ``label``."""
        self._ensure()
        if not value:
            return False
        locator = self._page.get_by_label(label, exact=False)
        try:
            if locator.count() == 0:
                return False
            locator.first.fill(value)
            return True
        except Exception:
            return False

    def upload_resume(self, path: Path) -> bool:
        """Set the first file input to ``path``."""
        self._ensure()
        locator = self._page.locator("input[type='file']")
        try:
            if locator.count() == 0:
                return False
            locator.first.set_input_files(str(path))
            return True
        except Exception:
            return False

    def question_labels(self) -> list[str]:
        """Return labels of empty text inputs and text areas."""
        self._ensure()
        script = """
        () => {
          const labels = [];
          for (const field of document.querySelectorAll("input, textarea, select")) {
            const type = (field.getAttribute("type") || "").toLowerCase();
            if (["hidden", "file", "submit", "button", "checkbox", "radio"].includes(type)) {
              continue;
            }
            if (field.value && field.value.trim()) {
              continue;
            }
            const id = field.getAttribute("id");
            let label = "";
            if (id) {
              const node = document.querySelector(`label[for="${id}"]`);
              if (node) label = node.innerText || "";
            }
            if (!label && field.getAttribute("aria-label")) {
              label = field.getAttribute("aria-label");
            }
            label = (label || "").replace(/\\s+/g, " ").trim();
            if (label) labels.push(label);
          }
          return labels;
        }
        """
        try:
            found = self._page.evaluate(script)
        except Exception:
            return []
        if not isinstance(found, list):
            return []
        return [str(item) for item in found if str(item).strip()]

    def answer_text_question(self, label: str, value: str) -> bool:
        """Fill a still-empty question. Selects are left for a person."""
        return self.fill_labeled(label, value)

    def click_submit(self) -> bool:
        """Click a submit button when one is visible."""
        self._ensure()
        for name in ("Submit application", "Submit", "Send application"):
            locator = self._page.get_by_role("button", name=name, exact=False)
            try:
                if locator.count() == 0:
                    continue
                locator.first.click()
                return True
            except Exception:
                continue
        return False

    def pause(self, reason: str) -> None:
        """Block on the configured pause callback."""
        self._pause(reason)

    def close(self) -> None:
        """Close the persistent context and stop Playwright."""
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.close()
            self._context = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                self._playwright.stop()
            self._playwright = None
        self._page = None

    def _ensure(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BoardError(
                "Playwright is not installed. Run pip install -e '.[browser]' "
                "and then `playwright install chromium`."
            ) from exc
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=self._headless,
        )
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()


@register_board
class GreenhouseBoard(BaseJobBoard):
    """Search public Greenhouse boards and fill their application forms.

    Args:
        board_tokens: Public board slugs, such as ``stripe`` or ``airbnb``.
        session_factory: Builds a page session. Defaults to persistent Chromium.
        transport: Optional httpx transport. Tests use this to avoid the network.
        profile_dir: Playwright user-data directory.
        headless: Hide the browser window.
        pause: Called when a person must intervene.
    """

    name = "greenhouse"

    def __init__(
        self,
        board_tokens: list[str],
        *,
        session_factory: Callable[[], PageSession] | None = None,
        transport: httpx.BaseTransport | None = None,
        profile_dir: Path | None = None,
        headless: bool = False,
        pause: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(pause=pause)
        self._tokens = [token.strip().strip("/") for token in board_tokens if token.strip()]
        self._session_factory = session_factory
        self._transport = transport
        self._profile_dir = profile_dir or Path("output/browser_profile")
        self._headless = headless

    def search(self, query: SearchQuery) -> list[JobListing]:
        """List jobs from each configured board token and keep the ones that match.

        One HTTP request is made per token. Results are filtered with the role
        keywords before they are returned, so a later scoring call is not spent
        on unrelated openings.

        Raises:
            BoardError: No board tokens are configured, or the API request fails.
        """
        if not self._tokens:
            raise BoardError(
                "No Greenhouse boards are configured. Set preferences.greenhouse_boards "
                "in profile.yaml, for example [stripe, airbnb]."
            )
        listings: list[JobListing] = []
        with httpx.Client(transport=self._transport, timeout=20.0, follow_redirects=True) as client:
            for token in self._tokens:
                listings.extend(self._fetch_token(client, token))
                if len(listings) >= query.limit:
                    break
        matched = [
            job
            for job in listings
            if _location_ok(job, query)
            and keyword_match(job.title, job.description, query.keyword_list(), query.text)
        ]
        return matched[: query.limit]

    def apply(self, job: JobListing, context: ApplyContext) -> ApplyResult:
        """Open the Greenhouse form, upload the role PDF, and answer what is known.

        The browser closes after the attempt. A pause happens before close, so
        the window is still available while a person deals with a CAPTCHA or reviews
        the form.
        """
        if not context.pdf_path.is_file():
            raise BoardError(f"Tailored CV PDF is missing: {context.pdf_path}")
        session = self._open_session()
        try:
            session.open(job.url)
            reason = needs_human(session.current_url(), session.body_text())
            if reason:
                session.pause(reason)
                return ApplyResult(status="pending_review", notes=reason)
            self._fill_profile(session, context)
            if not session.upload_resume(context.pdf_path):
                note = "The résumé file input was not found. Attach the PDF by hand."
                session.pause(note)
                return ApplyResult(status="pending_review", notes=note)
            for label in session.question_labels():
                answer = context.answer(label)
                if answer.needs_review or not answer.answer:
                    note = f"Needs a person: {label}"
                    session.pause(note)
                    return ApplyResult(status="pending_review", notes=note)
                session.answer_text_question(label, answer.answer)
            if context.dry_run or not context.auto_submit:
                note = "Form filled with the role PDF. Submit was not clicked."
                if not context.dry_run:
                    session.pause(
                        "Review the form in the browser, submit it if it looks right, "
                        "then press Enter."
                    )
                    return ApplyResult(status="pending_review", notes=note)
                return ApplyResult(status="dry_run", notes=note)
            if session.click_submit():
                return ApplyResult(status="submitted", notes="Submit control was clicked.")
            note = "Submit control was not found."
            session.pause(note)
            return ApplyResult(status="pending_review", notes=note)
        finally:
            session.close()

    def _open_session(self) -> PageSession:
        if self._session_factory is not None:
            return self._session_factory()
        return PlaywrightPageSession(
            self._profile_dir,
            headless=self._headless,
            pause=self._pause,
        )

    def _fill_profile(self, session: PageSession, context: ApplyContext) -> None:
        profile = context.profile
        values = {
            "First Name": profile.first_name,
            "Last Name": profile.last_name,
            "Email": profile.email,
            "Phone": profile.phone,
            "LinkedIn Profile": profile.linkedin_url,
            "Website": profile.portfolio_url,
            "Location": profile.location,
        }
        for label in _PROFILE_LABELS:
            value = values.get(label, "")
            if value:
                session.fill_labeled(label, value)

    def _fetch_token(self, client: httpx.Client, token: str) -> list[JobListing]:
        url = _API.format(token=token)
        try:
            response = client.get(url, params={"content": "true"})
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise BoardError(f"Greenhouse board '{token}' could not be listed: {exc}") from exc
        except ValueError as exc:
            raise BoardError(f"Greenhouse board '{token}' returned invalid JSON.") from exc
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise BoardError(f"Greenhouse board '{token}' did not return a jobs list.")
        company = token.replace("-", " ").title()
        listings: list[JobListing] = []
        for item in jobs:
            if not isinstance(item, dict):
                continue
            listing = _listing_from_api(item, company)
            if listing is not None:
                listings.append(listing)
        return listings


def _listing_from_api(item: dict[str, Any], company: str) -> JobListing | None:
    title = str(item.get("title") or "").strip()
    link = str(item.get("absolute_url") or "").strip()
    if not title or not link.startswith(("http://", "https://")):
        return None
    location = ""
    raw_location = item.get("location")
    if isinstance(raw_location, dict):
        location = str(raw_location.get("name") or "")
    description = html_to_text(str(item.get("content") or ""))
    return JobListing(
        title=title,
        company=company,
        url=link,
        location=location,
        description=description,
        board="greenhouse",
        external_id=str(item.get("id") or ""),
    )


def _location_ok(job: JobListing, query: SearchQuery) -> bool:
    if not query.remote_only:
        return True
    location = job.location.lower()
    if "remote" in location:
        return True
    wanted = query.location.strip().lower()
    return bool(wanted) and wanted in location
