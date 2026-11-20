"""Functional tests for each application type.

Each test opens a scripted job page, runs the real filler, and checks two
things: every contact field was written, and the application moved on.

The contact contract is ``expected_values``. Adding a field there means every
application type must show that field filled before the run is allowed to
continue. The scripted pages keep Next, Continue, and Submit disabled until
the phone field has a value.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from src.config import Settings
from src.errors import ConfigError
from src.job_applier import JobApplier, JobPosting, label_profile_tag
from src.models import Applicant, ManifestRole
from src.tracking import ApplicationLog
from tests.scripted_browser import ScriptedPage, application_page

BOARDS = ["indeed", "linkedin", "greenhouse"]


class FlowModel:
    """Answers the runner's JSON calls from the field labels. No network."""

    def complete_json(self, system: str, user: str, **kwargs: object) -> dict:
        if "dealbreakers" in system:
            return {"score": 92, "summary": "The CV matches this role.", "dealbreakers": []}
        if "Use only these tags" in system or "missing" in system.split("\n", 1)[0]:
            return {"fields": _tags_for(user)}
        if "needs_review" in system:
            return {"answer": "", "needs_review": True}
        return {"fields": []}


def _tags_for(user: str) -> list[dict[str, object]]:
    payload = json.loads(user.split("\n\n", 1)[0])
    tagged = []
    for row in payload.get("fields", []):
        label = str(row.get("label") or "")
        tagged.append({"id": row["id"], "tag": label_profile_tag(label) or "skip"})
    return tagged


def expected_values(applicant: Applicant) -> dict[str, str]:
    """Profile values the form must contain before the application continues.

    Add a field here when the profile grows. Each application page already
    has an input with that id, and the test fails until the filler writes it.
    """
    return {
        "first_name": applicant.first_name,
        "last_name": applicant.last_name,
        "email": applicant.email,
        "phone_prefix": applicant.phone_prefix,
        "phone": applicant.phone_number,
    }


def _applicant(**overrides: str) -> Applicant:
    values = {
        "full_name": "Ana Fandiño",
        "email": "ana@example.com",
        "phone": "+57 319 482 6159",
        "linkedin_url": "https://linkedin.com/in/ana",
        "location": "Bogotá, Colombia",
    }
    values.update(overrides)
    return Applicant(**values)


def _runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobApplier:
    import src.job_applier as job_applier

    monkeypatch.setattr(job_applier.sys, "stdin", io.StringIO(""))
    settings = Settings(
        min_delay_seconds=0,
        max_delay_seconds=0,
        min_between_applications=0,
        max_between_applications=0,
        headless=True,
    )
    log = ApplicationLog(tmp_path / "applications")
    return JobApplier(settings, FlowModel(), log, Console(file=io.StringIO()))


def _role(tmp_path: Path) -> tuple[ManifestRole, Path, str]:
    pdf = tmp_path / "software-engineer_cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    role = ManifestRole(
        title="Software Engineer",
        slug="software-engineer",
        pdf_path=str(pdf),
        html_path=str(tmp_path / "software-engineer_cv.html"),
        metadata_path="",
        keywords=["Python"],
        location="Remote",
    )
    cv_text = "Ana Fandiño builds internal tools with Python and ships them to production. " * 3
    return role, pdf, cv_text


def _run(runner: JobApplier, page: ScriptedPage, role: ManifestRole, pdf: Path, cv_text: str, applicant: Applicant, board: str):
    job = JobPosting(
        title="Software Engineer",
        company="Northwind",
        url=page.url,
        description="",
        board=board,
    )
    return runner._run_one_job(
        page,
        job,
        role,
        applicant,
        cv_text,
        pdf,
        dry_run=False,
        auto_submit=True,
        threshold=70,
    )


@pytest.mark.parametrize("board", BOARDS)
def test_contact_fields_are_filled_and_the_application_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board: str):
    applicant = _applicant()
    page = application_page(board)
    runner = _runner(tmp_path, monkeypatch)
    role, pdf, cv_text = _role(tmp_path)

    record = _run(runner, page, role, pdf, cv_text, applicant, board)

    assert record.status == "submitted"
    assert page.submitted is True
    assert page.events[-1] == "submit"
    for element_id, value in expected_values(applicant).items():
        control = page.node_by_id(element_id)
        assert control is not None, element_id
        assert control.value == value
    resume = page.node_by_id("resume")
    assert resume is not None and resume.files.endswith("software-engineer_cv.pdf")
    if board == "indeed":
        assert page.events == ["submit"]
    elif board == "greenhouse":
        assert page.events == ["next", "submit"]
        assert _inside(page.node_by_id("phone"), "application--questions") is False
    else:
        assert page.events == ["easy_apply", "next", "submit"]


@pytest.mark.parametrize("board", BOARDS)
def test_missing_phone_stops_the_application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board: str):
    page = application_page(board)
    runner = _runner(tmp_path, monkeypatch)
    role, pdf, cv_text = _role(tmp_path)

    with pytest.raises(ConfigError):
        _run(runner, page, role, pdf, cv_text, _applicant(phone=""), board)

    assert page.submitted is False
    assert "submit" not in page.events
    phone = page.node_by_id("phone")
    assert phone is not None and phone.value == ""


def _inside(node, class_name: str) -> bool:
    current = node
    while current is not None:
        if class_name in current.attrs.get("class", "").split():
            return True
        current = current.parent
    return False
