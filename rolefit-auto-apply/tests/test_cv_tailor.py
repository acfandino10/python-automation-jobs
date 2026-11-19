"""Tests for grounded tailoring and PDF rendering. No live model calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rolefit.exceptions import CVError
from rolefit.llm import parse_json_object
from rolefit.models import Applicant
from rolefit.parser import extract_text
from rolefit.tailor import (
    build_tailored_cv,
    generate_tailored_cvs,
    render_html,
    render_pdf,
    suggest_roles,
)
from tests.conftest import SOURCE, ScriptedLLM

GROUNDED: dict[str, Any] = {
    "target_role": "Ignored",
    "full_name": "Someone Else",
    "headline": "Software Engineer",
    "contact_line": "wrong@example.com",
    "summary": "Engineer who ships internal reporting tools.",
    "skills": ["Python", "Kubernetes", "SQL"],
    "experience": [
        {
            "title": "Software Engineer",
            "company": "Northwind Analytics",
            "location": "Remote",
            "dates": "2018-2024",
            "bullets": [
                "Built internal reporting tools used by 12 analysts.",
                "Raised revenue 40%.",
            ],
        },
        {
            "title": "CTO",
            "company": "Initech",
            "dates": "1999-2001",
            "bullets": ["Invented a platform."],
        },
    ],
    "education": [
        {
            "degree": "B.S. Computer Science",
            "school": "River College",
            "dates": "2014-2018",
            "details": [],
        },
        {"degree": "MBA", "school": "Hogwarts", "dates": "1999", "details": ["GPA 4.0"]},
    ],
    "projects": [
        {"name": "Harbor Export", "details": ["Python extract tool."]},
        {"name": "Secret Rocket", "details": ["Classified."]},
    ],
    "search_keywords": ["Python", "Kubernetes", "Staff Platform Engineer"],
}


def test_parse_json_object_accepts_a_fence() -> None:
    payload = parse_json_object('```json\n{"score": 81, "summary": "Strong fit"}\n```')
    assert payload["score"] == 81


def test_grounding_drops_invented_claims(tmp_path: Path, applicant: Applicant) -> None:
    cv = build_tailored_cv(GROUNDED, SOURCE, applicant, "Staff Platform Engineer")
    assert cv.full_name == "Jordan Lee"
    assert "jordan@example.com" in cv.contact_line
    assert [job.company for job in cv.experience] == ["Northwind Analytics"]
    assert cv.experience[0].bullets == ["Built internal reporting tools used by 12 analysts."]
    assert cv.experience[0].dates == "2018-2024"
    assert cv.skills == ["Python", "SQL"]
    assert [item.school for item in cv.education] == ["River College"]
    assert [item.name for item in cv.projects] == ["Harbor Export"]
    assert cv.search_keywords[0] == "Staff Platform Engineer"
    assert "Kubernetes" not in cv.search_keywords

    html = render_html(cv)
    assert "Northwind Analytics" in html
    assert "Initech" not in html
    assert "Hogwarts" not in html

    pdf_path = tmp_path / "tailored.pdf"
    engine = render_pdf(cv, html, pdf_path, engine="reportlab")
    assert engine == "reportlab"
    extracted = extract_text(pdf_path)
    assert "Northwind Analytics" in extracted
    assert "Initech" not in extracted
    assert "Jordan Lee" in extracted


def test_grounding_rejects_a_cv_full_of_invented_employers(applicant: Applicant) -> None:
    with pytest.raises(CVError):
        build_tailored_cv(
            {
                "target_role": "Software Engineer",
                "full_name": "Jordan Lee",
                "experience": [
                    {"title": "CTO", "company": "Initech", "bullets": ["Did everything."]}
                ],
            },
            SOURCE,
            applicant,
            "Software Engineer",
        )


def test_suggest_and_generate_write_one_pdf_per_role(tmp_path: Path, applicant: Applicant) -> None:
    suggester = ScriptedLLM(
        {
            "roles": [
                {
                    "title": "Senior Python Engineer",
                    "reason": "Ships Python reporting tools at Northwind Analytics.",
                    "search_keywords": ["Python", "SQL"],
                },
                {
                    "title": "Data Engineer",
                    "reason": "Built reporting pipelines used by analysts.",
                    "search_keywords": ["SQL"],
                },
            ]
        }
    )
    roles = suggest_roles(suggester, SOURCE, count=6)
    assert [role.title for role in roles] == ["Senior Python Engineer", "Data Engineer"]
    assert suggester.calls == 1

    tailor = ScriptedLLM(GROUNDED)
    output = tmp_path / "tailored_cvs"
    manifest = generate_tailored_cvs(
        source_path=tmp_path / "base_cv.txt",
        source_text=SOURCE,
        titles=[role.title for role in roles],
        applicant=applicant,
        llm=tailor,
        output_dir=output,
        pdf_engine="reportlab",
        provider="scripted",
        model="scripted-1",
        location="Remote",
    )
    assert tailor.calls == 2
    slugs = [role.slug for role in manifest.roles]
    assert slugs == ["data-engineer", "senior-python-engineer"]
    for role in manifest.roles:
        pdf = Path(role.pdf_path)
        assert pdf.name == f"{role.slug}_cv.pdf"
        assert pdf.is_file()
        assert "Northwind Analytics" in extract_text(pdf)
        assert "Initech" not in extract_text(pdf)
        assert "Kubernetes" not in role.keywords
    saved = (output / "manifest.json").read_text(encoding="utf-8")
    assert "senior-python-engineer_cv.pdf" in saved
