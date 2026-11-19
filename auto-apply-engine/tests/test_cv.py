from pathlib import Path

import pytest
from docx import Document
from reportlab.pdfgen import canvas

from src.cv_parser import extract_text
from src.cv_tailor import build_tailored_cv, render_html, render_pdf
from src.errors import CVError
from src.models import Applicant, slugify, unique_slug


SOURCE = """
Jordan Lee
jordan@example.com

Software Engineer at Northwind Analytics from 2018 to 2024.
Built internal reporting tools used by 12 analysts.
Skills: Python, SQL, React.
Education: B.S. Computer Science, River College, 2014 to 2018.
Project: Harbor Export, a Python extract tool.
"""


def test_contact_comes_from_the_cv_and_ignores_a_shared_profile():
    from src.people import resolve_applicant

    text = """
Jordan Lee
jordan@example.com | +1 555 0100 | Toronto, ON | https://linkedin.com/in/jordan
https://github.com/jordan
Authorized to work in Canada and does not require sponsorship.
Salary expectation: Negotiable
Software engineer with several years of Python and SQL work.
"""
    shared = Applicant(
        full_name="From Env",
        email="env@example.com",
        phone="999",
        location="Remote",
        work_authorization="From the environment",
        salary_expectation="From the environment",
    )
    person = resolve_applicant(text, Path("ignored.profile"), shared, solo=True)
    assert person.full_name == "Jordan Lee"
    assert person.email == "jordan@example.com"
    assert "555" in person.phone
    assert person.linkedin_url == "https://linkedin.com/in/jordan"
    assert person.portfolio_url == "https://github.com/jordan"
    assert person.location == "Toronto, ON"
    assert "Canada" in person.work_authorization
    assert person.salary_expectation == "Salary expectation: Negotiable"
    assert person.full_name != shared.full_name
    assert person.email != shared.email


def test_slug_is_stable_and_unique():
    assert slugify("Senior Python Engineer") == "senior-python-engineer"
    used: set[str] = set()
    first = unique_slug("Data Engineer", used)
    second = unique_slug("Data Engineer", used)
    assert first == "data-engineer"
    assert second == "data-engineer-2"


def test_parser_reads_pdf_and_docx(tmp_path: Path):
    pdf_path = tmp_path / "resume.pdf"
    sheet = canvas.Canvas(str(pdf_path))
    sheet.drawString(72, 720, "Jordan Lee built Python tools at Northwind Analytics")
    sheet.save()
    assert "Northwind Analytics" in extract_text(pdf_path)

    docx_path = tmp_path / "resume.docx"
    document = Document()
    document.add_paragraph("Jordan Lee")
    document.add_paragraph("Software Engineer at Northwind Analytics")
    document.save(docx_path)
    assert "Northwind Analytics" in extract_text(docx_path)


def test_grounding_drops_invented_claims(tmp_path: Path):
    applicant = Applicant(
        full_name="Jordan Lee",
        email="jordan@example.com",
        phone="+1 555 0100",
        location="Remote",
    )
    cv = build_tailored_cv(
        {
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
                {"degree": "B.S. Computer Science", "school": "River College", "dates": "2014-2018", "details": []},
                {"degree": "MBA", "school": "Hogwarts", "dates": "1999", "details": ["GPA 4.0"]},
            ],
            "projects": [
                {"name": "Harbor Export", "details": ["Python extract tool."]},
                {"name": "Secret Rocket", "details": ["Classified."]},
            ],
            "search_keywords": ["Python", "Kubernetes", "Staff Platform Engineer"],
        },
        SOURCE,
        applicant,
        "Staff Platform Engineer",
    )

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


def test_grounding_rejects_a_cv_full_of_invented_employers():
    with pytest.raises(CVError):
        build_tailored_cv(
            {
                "target_role": "Software Engineer",
                "full_name": "Jordan Lee",
                "experience": [{"title": "CTO", "company": "Initech", "bullets": ["Did everything."]}],
            },
            SOURCE,
            Applicant(full_name="Jordan Lee"),
            "Software Engineer",
        )
