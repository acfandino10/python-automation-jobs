"""Tests for scoring, Greenhouse search, form filling, and the application log."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from rolefit.applier import build_boards, run_apply
from rolefit.boards.base import ApplyContext, ApplyResult, BaseJobBoard, SearchQuery
from rolefit.boards.greenhouse import GreenhouseBoard
from rolefit.config import CandidateProfile, Settings, WorkPreferences, load_profile, merge_profile
from rolefit.exceptions import ConfigError
from rolefit.forms import (
    canonical_url,
    is_agreement_question,
    is_eeo_question,
    keyword_match,
    needs_human,
    profile_value_for_label,
    yes_no_answer,
)
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
from tests.conftest import SOURCE


class FixedScorer:
    """Score retail postings low and every other posting high."""

    def __init__(self) -> None:
        self.calls = 0

    def score(self, cv: TailoredCV, job: JobListing) -> JobFit:
        self.calls += 1
        if "retail" in job.title.lower():
            return JobFit(score=10, summary="Little overlap with the tailored CV.")
        return JobFit(score=90, summary="Strong overlap with the tailored CV.")


class RecordingBoard(BaseJobBoard):
    """In-memory board that records the PDF path it was asked to upload."""

    name = "recording"

    def __init__(self, jobs: list[JobListing]) -> None:
        super().__init__(pause=lambda _reason: None)
        self._jobs = jobs
        self.applied: list[Path] = []

    def search(self, query: SearchQuery) -> list[JobListing]:
        return list(self._jobs)

    def apply(self, job: JobListing, context: ApplyContext) -> ApplyResult:
        self.applied.append(context.pdf_path)
        status = "dry_run" if context.dry_run else "submitted"
        return ApplyResult(status=status, notes="filled")


class ScriptedPage:
    """Browser double used by the Greenhouse adapter tests."""

    def __init__(self, body: str, url: str) -> None:
        self.body = body
        self.url = url
        self.uploaded: Path | None = None
        self.filled: list[tuple[str, str]] = []
        self.pauses: list[str] = []
        self.questions: list[str] = []
        self.submitted = False
        self.closed = False

    def open(self, url: str) -> None:
        self.url = url

    def body_text(self) -> str:
        return self.body

    def current_url(self) -> str:
        return self.url

    def fill_labeled(self, label: str, value: str) -> bool:
        self.filled.append((label, value))
        return True

    def upload_resume(self, path: Path) -> bool:
        self.uploaded = path
        return True

    def question_labels(self) -> list[str]:
        return list(self.questions)

    def answer_text_question(self, label: str, value: str) -> bool:
        self.filled.append((label, value))
        return True

    def click_submit(self) -> bool:
        self.submitted = True
        return True

    def pause(self, reason: str) -> None:
        self.pauses.append(reason)

    def close(self) -> None:
        self.closed = True


def _entry(url: str, status: ApplicationStatus, score: float | None = 80) -> ApplicationRecord:
    return ApplicationRecord(
        job_title="Backend Engineer",
        company="Northwind",
        url=url,
        cv_used="output/tailored_cvs/backend-engineer_cv.pdf",
        match_score=score,
        timestamp=utc_now(),
        status=status,
        notes="",
    )


def _cv() -> TailoredCV:
    return TailoredCV(
        target_role="Backend Engineer",
        full_name="Jordan Lee",
        summary="Engineer who ships internal reporting tools.",
        skills=["Python", "SQL"],
        search_keywords=["Python", "SQL"],
    )


def _role(tmp_path: Path) -> ManifestRole:
    pdf = tmp_path / "backend-engineer_cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 500)
    return ManifestRole(
        title="Backend Engineer",
        slug="backend-engineer",
        pdf_path=str(pdf),
        keywords=["Python", "SQL"],
        location="Remote",
    )


def test_log_blocks_submissions_and_exports_csv(tmp_path: Path) -> None:
    log = ApplicationTracker(tmp_path)
    assert log.record(_entry("https://jobs.example/1", "dry_run", 70))
    assert not log.is_blocked("https://jobs.example/1")

    assert log.record(_entry("https://jobs.example/1", "submitted", 91))
    assert log.is_blocked("https://jobs.example/1")
    assert not log.record(_entry("https://jobs.example/1", "dry_run", 10))

    log.close()
    reopened = ApplicationTracker(tmp_path)
    assert reopened.is_blocked("https://jobs.example/1")
    text = (tmp_path / "applications.csv").read_text(encoding="utf-8")
    assert "Job Title,Company,URL,CV Used,Match Score,Timestamp,Status,Notes" in text
    assert "submitted" in text
    assert "https://jobs.example/1" in text
    reopened.close()


def test_deleting_a_csv_row_allows_the_url_again(tmp_path: Path) -> None:
    log = ApplicationTracker(tmp_path)
    log.record(_entry("https://jobs.example/2", "pending_review"))
    log.record(_entry("https://jobs.example/3", "below_threshold", 40))
    log.close()

    csv_path = tmp_path / "applications.csv"
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if "jobs.example/2" not in line]
    csv_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    reopened = ApplicationTracker(tmp_path)
    assert not reopened.is_blocked("https://jobs.example/2")
    assert any(record.url.endswith("/3") for record in reopened.all_records())
    reopened.close()


def test_profile_values_work_authorization_and_human_checks(applicant: Applicant) -> None:
    assert profile_value_for_label("First Name", applicant) == "Jordan"
    assert profile_value_for_label("Last Name", applicant) == "Lee"
    assert profile_value_for_label("Email address", applicant) == "jordan@example.com"
    assert profile_value_for_label("LinkedIn URL", applicant) == "https://linkedin.com/in/jordan"
    assert (
        yes_no_answer(
            "Will you now or in the future require sponsorship?",
            applicant.work_authorization,
        )
        == "No"
    )
    assert (
        yes_no_answer(
            "Are you authorized to work in the United States?",
            applicant.work_authorization,
        )
        is None
    )
    assert (
        yes_no_answer("Are you legally authorized to work?", applicant.work_authorization) == "Yes"
    )
    assert needs_human("https://www.indeed.com/jobs", "Please verify you are human")
    assert needs_human("https://www.linkedin.com/login", "Welcome")
    assert needs_human("https://example.com/jobs/1", "Python role") is None
    assert is_eeo_question("Gender (voluntary)")
    assert is_agreement_question("I agree to the privacy policy")
    assert keyword_match(
        "Backend Engineer", "We use Python and SQL.", ["Python"], "Backend Engineer"
    )
    assert not keyword_match(
        "Retail Associate", "Fold shirts.", ["Python", "SQL"], "Backend Engineer"
    )
    raw = "https://www.indeed.com/viewjob?jk=abc123&utm_source=google&from=serp"
    assert canonical_url(raw) == "https://www.indeed.com/viewjob?jk=abc123"


def test_search_settings_do_not_replace_cv_identity(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(
        "full_name: Ada Lovelace\n"
        "email: ada@example.com\n"
        "preferences:\n"
        "  search_boards: [greenhouse]\n",
        encoding="utf-8",
    )
    profile = load_profile(path)
    merged = merge_profile(Settings(search_boards="greenhouse", job_location="Berlin"), profile)
    assert merged.full_name == "Ada Lovelace"
    assert merged.email == "ada@example.com"
    assert merged.preferences.search_boards == ["greenhouse"]
    assert merged.preferences.job_location == "Berlin"


def test_unknown_board_is_rejected() -> None:
    profile = CandidateProfile(preferences=WorkPreferences(search_boards=["monster"]))
    with pytest.raises(ConfigError, match="Unknown search board"):
        build_boards(profile, Settings())


def test_greenhouse_search_filters_and_apply_uploads_the_role_pdf(
    tmp_path: Path,
    applicant: Applicant,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/boards/northwind/jobs")
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 10,
                        "title": "Backend Engineer",
                        "absolute_url": "https://boards.greenhouse.io/northwind/jobs/10",
                        "location": {"name": "Remote"},
                        "content": "<p>We use Python and SQL every day.</p>",
                    },
                    {
                        "id": 11,
                        "title": "Retail Associate",
                        "absolute_url": "https://boards.greenhouse.io/northwind/jobs/11",
                        "location": {"name": "Remote"},
                        "content": "<p>Fold shirts.</p>",
                    },
                ]
            },
        )

    page = ScriptedPage(
        body="Apply for this role", url="https://boards.greenhouse.io/northwind/jobs/10"
    )
    board = GreenhouseBoard(
        ["northwind"],
        session_factory=lambda: page,
        transport=httpx.MockTransport(handler),
        pause=page.pause,
    )
    found = board.search(
        SearchQuery(text="Backend Engineer", keywords=["Python", "SQL"], remote_only=True, limit=5)
    )
    assert [job.title for job in found] == ["Backend Engineer"]
    pdf = tmp_path / "backend-engineer_cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 500)
    result = board.apply(
        found[0],
        ApplyContext(
            profile=applicant,
            cv=_cv(),
            pdf_path=pdf,
            dry_run=True,
            auto_submit=False,
            answer=lambda _question: ScreeningAnswer(answer="5", needs_review=False),
        ),
    )
    assert result.status == "dry_run"
    assert page.uploaded == pdf
    assert ("First Name", "Jordan") in page.filled
    assert ("Email", "jordan@example.com") in page.filled
    assert page.submitted is False
    assert page.closed is True


def test_greenhouse_pauses_for_captcha_and_demographic_questions(
    tmp_path: Path,
    applicant: Applicant,
) -> None:
    captcha = ScriptedPage(
        body="Please verify you are human before continuing",
        url="https://boards.greenhouse.io/northwind/jobs/10",
    )
    board = GreenhouseBoard(["northwind"], session_factory=lambda: captcha, pause=captcha.pause)
    pdf = tmp_path / "backend-engineer_cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 500)
    job = JobListing(
        title="Backend Engineer",
        company="Northwind",
        url="https://boards.greenhouse.io/northwind/jobs/10",
        description=SOURCE,
        board="greenhouse",
    )
    paused = board.apply(
        job,
        ApplyContext(
            profile=applicant,
            cv=_cv(),
            pdf_path=pdf,
            dry_run=True,
            auto_submit=False,
            answer=lambda _question: ScreeningAnswer(answer="", needs_review=False),
        ),
    )
    assert paused.status == "pending_review"
    assert captcha.pauses
    assert captcha.uploaded is None

    questions = ScriptedPage(body="Application", url=job.url)
    questions.questions = ["Gender (voluntary)"]
    review_board = GreenhouseBoard(
        ["northwind"],
        session_factory=lambda: questions,
        pause=questions.pause,
    )

    def answer(question: str) -> ScreeningAnswer:
        from rolefit.forms import answer_from_profile

        local = answer_from_profile(question, applicant)
        assert local is not None
        return local

    reviewed = review_board.apply(
        job,
        ApplyContext(
            profile=applicant,
            cv=_cv(),
            pdf_path=pdf,
            dry_run=False,
            auto_submit=True,
            answer=answer,
        ),
    )
    assert reviewed.status == "pending_review"
    assert questions.submitted is False
    assert any("Gender" in note for note in questions.pauses)


def test_apply_skips_low_scores_and_never_submits_the_same_url_twice(
    tmp_path: Path, applicant: Applicant
) -> None:
    role = _role(tmp_path)
    first = JobListing(
        title="Backend Engineer",
        company="Northwind",
        url="https://jobs.example/backend?utm_source=board",
        description="Python and SQL",
        board="recording",
    )
    second = JobListing(
        title="Backend Engineer",
        company="Northwind",
        url="https://jobs.example/backend",
        description="Python and SQL",
        board="recording",
    )
    weak = JobListing(
        title="Retail Associate",
        company="Northwind",
        url="https://jobs.example/retail",
        description="Fold shirts",
        board="recording",
    )
    board = RecordingBoard([first, second, weak])
    scorer = FixedScorer()
    low = FixedScorer()

    def load_cv(_role: ManifestRole) -> TailoredCV:
        return _cv()

    with ApplicationTracker(tmp_path / "logs") as tracker:
        summary = run_apply(
            roles=[role],
            boards=[board],
            scorer=scorer,
            answerer=_UnusedAnswerer(),
            tracker=tracker,
            applicant=applicant,
            threshold=75,
            limit=5,
            dry_run=False,
            auto_submit=True,
            load_cv=load_cv,
            wait=lambda: None,
        )
        assert summary.attempted == 1
        assert board.applied == [Path(role.pdf_path)]
        assert scorer.calls == 2
        assert any(record.status == "below_threshold" for record in summary.records)
        assert tracker.is_blocked("https://jobs.example/backend")

        again = run_apply(
            roles=[role],
            boards=[RecordingBoard([first])],
            scorer=low,
            answerer=_UnusedAnswerer(),
            tracker=tracker,
            applicant=applicant,
            threshold=75,
            limit=5,
            dry_run=True,
            auto_submit=False,
            load_cv=load_cv,
            wait=lambda: None,
        )
        assert again.attempted == 0
        assert again.skipped == 1
        assert low.calls == 0


class _UnusedAnswerer:
    def answer(self, question: str, cv: TailoredCV, profile: Applicant) -> ScreeningAnswer:
        raise AssertionError(question)


def test_cli_help_lists_generate_and_apply() -> None:
    from rolefit.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "generate" in result.stdout
    assert "apply" in result.stdout
    version = runner.invoke(app, ["version"])
    assert version.exit_code == 0
    assert "0.1.0" in version.stdout
    boards = runner.invoke(app, ["boards"])
    assert boards.exit_code == 0
    assert "greenhouse" in boards.stdout
