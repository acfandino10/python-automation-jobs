"""Tests for résumé extraction and section parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document
from reportlab.pdfgen import canvas

from rolefit.exceptions import CVError
from rolefit.models import slugify, unique_slug
from rolefit.parser import ExtractorRegistry, extract_text, parse_cv, structure_text
from tests.conftest import SOURCE


def test_applicant_comes_from_cv_text() -> None:
    from rolefit.parser import applicant_from_text

    text = """
Jordan Lee
jordan@example.com | +1 555 0100 | Toronto, ON | https://linkedin.com/in/jordan
https://github.com/jordan
Authorized to work in Canada and does not require sponsorship.
Salary expectation: Negotiable

Summary
Software engineer who ships internal reporting tools for analysts each year.
"""
    person = applicant_from_text(text)
    assert person.full_name == "Jordan Lee"
    assert person.email == "jordan@example.com"
    assert "555" in person.phone
    assert person.linkedin_url == "https://linkedin.com/in/jordan"
    assert person.portfolio_url == "https://github.com/jordan"
    assert person.location == "Toronto, ON"
    assert "Canada" in person.work_authorization
    assert person.salary_expectation == "Salary expectation: Negotiable"


def test_slug_is_stable_and_unique() -> None:
    assert slugify("Senior Python Engineer") == "senior-python-engineer"
    used: set[str] = set()
    first = unique_slug("Data Engineer", used)
    second = unique_slug("Data Engineer", used)
    assert first == "data-engineer"
    assert second == "data-engineer-2"


def test_parser_reads_pdf_and_docx(tmp_path: Path) -> None:
    pdf_path = tmp_path / "resume.pdf"
    sheet = canvas.Canvas(str(pdf_path))
    sheet.drawString(72, 720, "Jordan Lee built Python tools at Northwind Analytics in 2018")
    sheet.save()
    assert "Northwind Analytics" in extract_text(pdf_path)

    docx_path = tmp_path / "resume.docx"
    document = Document()
    document.add_paragraph("Jordan Lee")
    document.add_paragraph("Software Engineer at Northwind Analytics from 2018 to 2024.")
    document.save(str(docx_path))
    assert "Northwind Analytics" in extract_text(docx_path)


def test_parser_reads_text_and_rejects_unknown_or_empty_files(tmp_path: Path) -> None:
    text_path = tmp_path / "resume.txt"
    text_path.write_text(SOURCE, encoding="utf-8")
    parsed = parse_cv(text_path)
    assert parsed.full_name == "Jordan Lee"
    assert parsed.email == "jordan@example.com"
    assert "Python" in parsed.skills
    assert parsed.experience[0].company == "Northwind Analytics"
    assert "12 analysts" in parsed.experience[0].bullets[0]
    assert parsed.education[0].school == "River College"
    assert parsed.projects[0].name == "Harbor Export"
    assert "Northwind Analytics" in parsed.raw_text

    empty = tmp_path / "empty.txt"
    empty.write_text("too short", encoding="utf-8")
    with pytest.raises(CVError):
        extract_text(empty)

    unknown = tmp_path / "resume.rtf"
    unknown.write_text(SOURCE, encoding="utf-8")
    with pytest.raises(CVError, match="Unsupported CV type"):
        extract_text(unknown)


def test_custom_extractor_is_used_for_its_suffix(tmp_path: Path) -> None:
    class NoteExtractor:
        suffixes: tuple[str, ...] = (".note",)

        def extract(self, path: Path) -> str:
            return path.read_text(encoding="utf-8")

    registry = ExtractorRegistry()
    registry.register(NoteExtractor())
    path = tmp_path / "resume.note"
    path.write_text(SOURCE, encoding="utf-8")
    parsed = parse_cv(path, registry)
    assert parsed.experience[0].company == "Northwind Analytics"


def test_structure_text_ignores_unknown_headings() -> None:
    data = structure_text(SOURCE + "\n\nHobbies\nChess\n")
    assert data.projects[0].name == "Harbor Export"
    assert all(item.company != "Chess" for item in data.experience)
