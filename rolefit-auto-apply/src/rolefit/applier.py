"""Score openings, fill applications with the matching role PDF, and record each attempt.

The runner is board-agnostic. It asks each :class:`~rolefit.boards.base.BaseJobBoard`
to search and apply, then writes the outcome to the :class:`~rolefit.tracker.ApplicationTracker`.
A URL already marked ``submitted`` or ``pending_review`` is skipped.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from rolefit.boards.base import ApplyContext, ApplyResult, BaseJobBoard, SearchQuery
from rolefit.boards.greenhouse import GreenhouseBoard
from rolefit.config import CandidateProfile, Settings
from rolefit.exceptions import BoardError, ConfigError, LLMError
from rolefit.forms import answer_from_profile, canonical_url
from rolefit.llm import LLMClient
from rolefit.models import (
    Applicant,
    ApplicationRecord,
    ApplicationStatus,
    JobFit,
    JobListing,
    ManifestRole,
    ScreeningAnswer,
    TailoredCV,
)
from rolefit.tracker import ApplicationTracker, utc_now

_SCORE_SYSTEM = """You compare a candidate CV with one job description.
Return one JSON object: {"score": 0, "summary": "", "dealbreakers": []}
score is an integer from 0 to 100 for how well the candidate's real
experience matches the role.
summary is one sentence.
dealbreakers lists required licenses, degrees, or skills the CV does not show.
An empty list is fine.
Do not assume the candidate has anything that is absent from the CV.
Nice-to-have skills are not dealbreakers.
Return JSON only.
"""

_ANSWER_SYSTEM = """You answer one job-application question from the candidate profile and CV.
Return one JSON object: {"answer": "", "needs_review": false}
Use only facts in the profile and CV. Do not invent employers, dates, metrics, or visa status.
If the answer is not in the material, set needs_review to true and answer to "".
Keep the answer under 400 characters.
Return JSON only.
"""

_STATUS_MAP: dict[str, ApplicationStatus] = {
    "submitted": "submitted",
    "pending_review": "pending_review",
    "dry_run": "dry_run",
    "below_threshold": "below_threshold",
    "skipped_manual": "skipped_manual",
    "error": "error",
}


class Scorer(Protocol):
    """Score one job against a tailored CV."""

    def score(self, cv: TailoredCV, job: JobListing) -> JobFit:
        """Return a 0-100 fit score."""


class Answerer(Protocol):
    """Answer one screening question."""

    def answer(self, question: str, cv: TailoredCV, profile: Applicant) -> ScreeningAnswer:
        """Return a short answer, or mark the question for a person."""


@dataclass
class ApplySummary:
    """Counts and rows produced by one apply run."""

    considered: int
    attempted: int
    skipped: int
    records: list[ApplicationRecord]


class LLMJobScorer:
    """Score a posting with the configured language model.

    The model is asked only after the caller has already decided the job is worth a call.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def score(self, cv: TailoredCV, job: JobListing) -> JobFit:
        """Return the model's fit score for this CV and job."""
        user = (
            f"Target role: {cv.target_role}\n\n"
            f"CV:\n{cv.as_text()[:8000]}\n\n"
            f"Job title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n\n"
            f"Job description:\n{job.description[:8000]}"
        )
        payload = self._llm.complete_json(_SCORE_SYSTEM, user, temperature=0.0, max_tokens=600)
        try:
            return JobFit.model_validate(payload)
        except ValidationError as exc:
            raise LLMError(f"The match score was not in the expected shape: {exc}") from exc


class LLMAnswerer:
    """Answer screening questions from the profile first, then from the model.

    Demographic questions, agreements, and government identifiers are never sent
    to the model.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def answer(self, question: str, cv: TailoredCV, profile: Applicant) -> ScreeningAnswer:
        """Return an answer grounded in the profile or the CV."""
        local = answer_from_profile(question, profile)
        if local is not None:
            return local
        user = (
            f"Question: {question}\n\n"
            f"Profile:\n{profile.model_dump_json()}\n\n"
            f"CV:\n{cv.as_text()[:8000]}"
        )
        payload = self._llm.complete_json(_ANSWER_SYSTEM, user, temperature=0.1, max_tokens=400)
        try:
            return ScreeningAnswer.model_validate(payload)
        except ValidationError as exc:
            raise LLMError(f"The screening answer was not in the expected shape: {exc}") from exc


def build_boards(
    profile: CandidateProfile,
    settings: Settings,
    *,
    pause: Callable[[str], None] | None = None,
) -> list[BaseJobBoard]:
    """Construct the adapters named in the profile.

    Greenhouse receives the configured board tokens and the persistent browser
    directory. Any other registered board is constructed with the shared pause
    callback. Unknown names raise :class:`ConfigError`.

    Args:
        profile: Candidate profile, including search preferences.
        settings: Runtime settings such as the browser directory.
        pause: Optional replacement for the terminal pause.
    """
    from rolefit.boards import available_boards, get_board

    names = _unique(profile.preferences.search_boards)
    if not names:
        raise ConfigError(
            "No search boards are configured. Set preferences.search_boards in profile.yaml."
        )
    known = set(available_boards())
    boards: list[BaseJobBoard] = []
    for name in names:
        if name not in known:
            registered = ", ".join(sorted(known)) or "(none)"
            message = f"Unknown search board '{name}'. Registered boards: {registered}."
            raise ConfigError(message)
        if name == "greenhouse":
            boards.append(
                GreenhouseBoard(
                    profile.preferences.greenhouse_boards,
                    profile_dir=settings.resolve(settings.browser_profile_dir),
                    headless=settings.headless,
                    pause=pause,
                )
            )
            continue
        boards.append(get_board(name)(pause=pause))
    return boards


def run_apply(
    *,
    roles: Sequence[ManifestRole],
    boards: Sequence[BaseJobBoard],
    scorer: Scorer,
    answerer: Answerer,
    tracker: ApplicationTracker,
    applicant: Applicant,
    threshold: int,
    limit: int,
    dry_run: bool,
    auto_submit: bool,
    load_cv: Callable[[ManifestRole], TailoredCV],
    remote_only: bool = False,
    wait: Callable[[], None] | None = None,
) -> ApplySummary:
    """Search, score, and apply for each selected role.

    ``limit`` counts application attempts (including dry runs), not jobs that
    were skipped as duplicates or below the match threshold. The same URL is
    handled at most once per run, and a URL already submitted is never applied
    to again.

    The PDF uploaded for a job is ``role.pdf_path`` for the role whose search
    found it.
    """
    records: list[ApplicationRecord] = []
    seen: set[str] = set()
    considered = 0
    attempted = 0
    skipped = 0
    pause_between = wait or _noop
    for role in roles:
        if attempted >= limit:
            break
        cv = load_cv(role)
        pdf_path = Path(role.pdf_path)
        for board in boards:
            if attempted >= limit:
                break
            query = SearchQuery(
                text=role.title,
                location=role.location,
                keywords=list(role.keywords),
                remote_only=remote_only,
                limit=max(limit, 1) * 4,
            )
            try:
                jobs = board.search(query)
            except BoardError as exc:
                skipped += 1
                records.append(
                    _store(
                        tracker,
                        _row(
                            title=role.title,
                            company=board.name,
                            url=f"board://{board.name}/{role.slug}",
                            cv_used=role.pdf_path,
                            score=None,
                            status="error",
                            notes=str(exc),
                        ),
                    )
                )
                continue
            for job in jobs:
                if attempted >= limit:
                    break
                url = canonical_url(job.url)
                considered += 1
                if url in seen or tracker.is_blocked(url):
                    skipped += 1
                    continue
                seen.add(url)
                fit = scorer.score(cv, job)
                if fit.score < threshold:
                    skipped += 1
                    note = fit.summary or "Below the match threshold."
                    if fit.dealbreakers:
                        note = f"{note} Dealbreakers: {', '.join(fit.dealbreakers)}."
                    records.append(
                        _store(
                            tracker,
                            _row(
                                title=job.title,
                                company=job.company,
                                url=url,
                                cv_used=role.pdf_path,
                                score=float(fit.score),
                                status="below_threshold",
                                notes=note,
                            ),
                        )
                    )
                    continue
                attempted += 1

                def _answer(question: str, current: TailoredCV = cv) -> ScreeningAnswer:
                    return answerer.answer(question, current, applicant)

                context = ApplyContext(
                    profile=applicant,
                    cv=cv,
                    pdf_path=pdf_path,
                    dry_run=dry_run,
                    auto_submit=auto_submit and not dry_run,
                    answer=_answer,
                )
                try:
                    result = board.apply(job, context)
                except BoardError as exc:
                    result = ApplyResult(status="error", notes=str(exc))
                records.append(
                    _store(
                        tracker,
                        _row(
                            title=job.title,
                            company=job.company,
                            url=url,
                            cv_used=str(pdf_path),
                            score=float(fit.score),
                            status=_coerce_status(result.status),
                            notes=result.notes,
                        ),
                    )
                )
                if attempted < limit:
                    pause_between()
    return ApplySummary(
        considered=considered,
        attempted=attempted,
        skipped=skipped,
        records=records,
    )


def _row(
    *,
    title: str,
    company: str,
    url: str,
    cv_used: str,
    score: float | None,
    status: ApplicationStatus,
    notes: str,
) -> ApplicationRecord:
    return ApplicationRecord(
        job_title=title,
        company=company,
        url=url,
        cv_used=cv_used,
        match_score=score,
        timestamp=utc_now(),
        status=status,
        notes=notes,
    )


def _store(tracker: ApplicationTracker, entry: ApplicationRecord) -> ApplicationRecord:
    tracker.record(entry)
    return entry


def _coerce_status(value: str) -> ApplicationStatus:
    return _STATUS_MAP.get(value, "error")


def _unique(names: list[str]) -> list[str]:
    ordered: list[str] = []
    for name in names:
        cleaned = name.strip().lower()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def _noop() -> None:
    return None


def between_applications(minimum: float, maximum: float) -> Callable[[], None]:
    """Return a waiter that sleeps a random interval inside the configured range.

    The delay is how Rolefit spaces submissions. Adapters should still avoid
    bursting listing requests.
    """
    import time

    low = max(0.0, min(minimum, maximum))
    high = max(minimum, maximum, 0.0)

    def _wait() -> None:
        time.sleep(random.uniform(low, high))

    return _wait
