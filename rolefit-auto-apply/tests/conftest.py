"""Shared fixtures for offline Rolefit tests."""

from __future__ import annotations

from typing import Any

import pytest

from rolefit.models import Applicant

SOURCE = """
Jordan Lee
jordan@example.com

Summary
Software engineer who ships internal reporting tools.

Skills
Python, SQL, React

Experience
Software Engineer — Northwind Analytics — 2018-2024
- Built internal reporting tools used by 12 analysts.

Education
B.S. Computer Science — River College — 2014-2018

Projects
Harbor Export
- A Python extract tool.
"""


class ScriptedLLM:
    """Return a fixed JSON object. Tests never call a live model."""

    provider = "scripted"
    model = "scripted-1"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        self.calls += 1
        return self.payload


@pytest.fixture
def source_text() -> str:
    """Return a résumé that the grounding tests can check against."""
    return SOURCE


@pytest.fixture
def applicant() -> Applicant:
    """Return the contact record used by tailoring and form-filling tests."""
    return Applicant(
        full_name="Jordan Lee",
        email="jordan@example.com",
        phone="+1 555 0100",
        linkedin_url="https://linkedin.com/in/jordan",
        portfolio_url="https://jordan.example",
        location="Remote",
        work_authorization="Authorized to work in Colombia and does not require sponsorship.",
    )
