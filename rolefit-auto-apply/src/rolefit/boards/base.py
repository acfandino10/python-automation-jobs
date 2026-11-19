"""Plugin interface for job-board adapters.

A contributor adds a board by subclassing :class:`BaseJobBoard`, setting
``name``, and decorating the class with :func:`register_board`. Importing the
module is enough for :func:`get_board` to find it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from rolefit.models import Applicant, JobListing, ScreeningAnswer, TailoredCV

console = Console()


def default_pause(reason: str) -> None:
    """Print why a person is needed and wait for Enter.

    The browser window stays open while this call blocks. Rolefit does not try
    to solve CAPTCHAs or click through a sign-in wall.
    """
    console.print(Panel(reason, title="Human needed", border_style="yellow"))
    input("Press Enter after you have finished in the browser. ")


@dataclass
class SearchQuery:
    """What an adapter should look for."""

    text: str
    location: str = ""
    keywords: list[str] | None = None
    remote_only: bool = False
    limit: int = 20

    def keyword_list(self) -> list[str]:
        """Return search keywords, omitting blanks."""
        return [item.strip() for item in (self.keywords or []) if item.strip()]


@dataclass
class ApplyContext:
    """Everything an adapter needs to fill one application."""

    profile: Applicant
    cv: TailoredCV
    pdf_path: Path
    dry_run: bool
    auto_submit: bool
    answer: Callable[[str], ScreeningAnswer]


@dataclass
class ApplyResult:
    """Outcome of one application attempt."""

    status: str
    notes: str = ""


class BaseJobBoard(ABC):
    """Strategy interface implemented by each job board.

    ``search`` should return public listings and must not submit anything.
    ``apply`` fills the form for ``context.pdf_path`` — the PDF generated for
    the role being searched — and pauses with :meth:`pause_for_human` when a
    CAPTCHA, login wall, or unanswered required question appears.

    Subclasses set :attr:`name` to a short lowercase id such as ``greenhouse``.
    """

    name: str = ""

    def __init__(self, pause: Callable[[str], None] | None = None) -> None:
        self._pause = pause or default_pause

    def pause_for_human(self, reason: str) -> None:
        """Block until a person handles a CAPTCHA, login, or review step.

        Args:
            reason: Sentence shown in the terminal. Say what the person should do.
        """
        self._pause(reason)

    @abstractmethod
    def search(self, query: SearchQuery) -> list[JobListing]:
        """Return openings that match the query.

        Implementations should cap the result at ``query.limit`` and avoid
        tight request loops. Prefer an official or public listing API over
        scraping a search results page.
        """

    @abstractmethod
    def apply(self, job: JobListing, context: ApplyContext) -> ApplyResult:
        """Fill the application for ``job`` using the PDF in ``context``.

        When ``context.dry_run`` is true, fill the form and do not submit it.
        When ``context.auto_submit`` is false, stop and ask a person to review
        the form before anything is sent.
        """


_REGISTRY: dict[str, type[BaseJobBoard]] = {}


def register_board(cls: type[BaseJobBoard]) -> type[BaseJobBoard]:
    """Register a board class under ``cls.name``.

    Use this as a class decorator. Registering the same name twice replaces the
    previous class, which lets tests install a fake adapter.
    """
    if not cls.name:
        raise ValueError(f"{cls.__name__} is missing a board name.")
    _REGISTRY[cls.name] = cls
    return cls


def available_boards() -> list[str]:
    """Return registered board names in sorted order."""
    return sorted(_REGISTRY)


def get_board(name: str) -> type[BaseJobBoard]:
    """Return the class registered for ``name``.

    Raises:
        KeyError: No adapter is registered under that name.
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(available_boards()) or "(none)"
        raise KeyError(f"Unknown job board '{name}'. Registered boards: {known}.") from exc
