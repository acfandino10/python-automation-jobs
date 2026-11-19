from pathlib import Path

from src.config import Settings
from src.cv_tailor import load_manifest, merge_manifest
from src.models import Applicant, ManifestRole
from src.people import (
    applicant_for_run,
    discover_cvs,
    extract_contact,
    person_runs,
    person_slug,
    profile_path_for,
    resolve_applicant,
    settings_for_person,
    tailored_output_dir,
)

ANA = """
Ana Example
Senior Software Developer
ana@example.com   |   linkedin.com/in/ana-example/   |   +57 319 482 6159   |   Bogotá, Colombia ·
remote
"""

CAMILA = """
Camila Example
Customer Experience Specialist
linkedin.com/in/camila-example
About Me
Results-driven specialist.
"""

SHARED = Applicant(
    full_name="Shared Person",
    email="shared@example.com",
    phone="+1 555 0100",
    location="Remote",
    work_authorization="Authorized to work in Canada.",
    salary_expectation="120000",
)


def test_discover_cvs_skips_sidecars_and_hidden_files(tmp_path: Path):
    (tmp_path / "Ana_Fandino_CV.pdf").write_bytes(b"%PDF")
    (tmp_path / "Camila_Fandino_CV.docx").write_bytes(b"PK")
    (tmp_path / "notes.txt").write_text("not a resume but accepted as text", encoding="utf-8")
    (tmp_path / "Ana_Fandino_CV.profile").write_text("EMAIL=ana@example.com\n", encoding="utf-8")
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    names = [path.name for path in discover_cvs(tmp_path)]
    assert names == ["Ana_Fandino_CV.pdf", "Camila_Fandino_CV.docx", "notes.txt"]


def test_person_slug_drops_a_trailing_cv_and_stays_unique():
    used: set[str] = set()
    assert person_slug(Path("Ana_Fandino_CV.pdf"), used) == "ana-fandino"
    assert person_slug(Path("Camila_Fandino_CV.pdf"), used) == "camila-fandino"
    collided: set[str] = set()
    assert person_slug(Path("Ana_CV.pdf"), collided) == "ana"
    assert person_slug(Path("Ana.pdf"), collided) == "ana-2"


def test_extract_contact_reads_the_header():
    contact = extract_contact(ANA)
    assert contact.full_name == "Ana Example"
    assert contact.email == "ana@example.com"
    assert contact.phone == "+57 319 482 6159"
    assert contact.linkedin_url == "https://linkedin.com/in/ana-example"
    assert contact.location == "Bogotá, Colombia"


def test_second_person_does_not_inherit_the_shared_identity(tmp_path: Path):
    contact = resolve_applicant(CAMILA, tmp_path / "missing.profile", SHARED, solo=False)
    assert contact.full_name == "Camila Example"
    assert contact.email == ""
    assert contact.phone == ""
    assert contact.linkedin_url == "https://linkedin.com/in/camila-example"
    assert contact.work_authorization == ""


def test_one_person_does_not_take_identity_from_the_shared_profile(tmp_path: Path):
    contact = resolve_applicant(ANA, tmp_path / "missing.profile", SHARED, solo=True)
    assert contact.full_name == "Ana Example"
    assert contact.email == "ana@example.com"
    assert contact.phone == "+57 319 482 6159"
    assert contact.work_authorization == ""
    assert contact.salary_expectation == ""


def test_sidecar_fills_blanks_without_replacing_the_cv(tmp_path: Path):
    profile = tmp_path / "Camila_Example_CV.profile"
    profile.write_text(
        "FULL_NAME=Someone Else\nEMAIL=camila@example.com\nPHONE=+1 555 0199\n",
        encoding="utf-8",
    )
    contact = resolve_applicant(CAMILA, profile, SHARED, solo=False)
    assert contact.full_name == "Camila Example"
    assert contact.email == "camila@example.com"
    assert contact.phone == "+1 555 0199"
    assert contact.linkedin_url == "https://linkedin.com/in/camila-example"
    assert profile_path_for(tmp_path / "Camila_Example_CV.pdf") == profile


def test_apply_fills_blanks_from_the_sidecar_only(tmp_path: Path):
    source = tmp_path / "Camila_Example_CV.pdf"
    source.write_bytes(b"%PDF")
    profile_path_for(source).write_text(
        "EMAIL=camila@example.com\nWORK_AUTHORIZATION=Authorized to work in Canada.\n",
        encoding="utf-8",
    )
    stored = Applicant(full_name="Camila Example", linkedin_url="https://linkedin.com/in/camila-example")
    ready = applicant_for_run(stored, source, SHARED)
    assert ready.full_name == "Camila Example"
    assert ready.email == "camila@example.com"
    assert ready.phone == ""
    assert ready.work_authorization == "Authorized to work in Canada."
    assert ready.location == ""


def test_each_person_gets_a_separate_output_directory():
    assert tailored_output_dir("ana-fandino", multiple=False, output_dir="output/tailored_cvs") == (
        "output/tailored_cvs"
    )
    assert tailored_output_dir("ana-fandino", multiple=True, output_dir="output/tailored_cvs") == (
        "output/people/ana-fandino/tailored_cvs"
    )
    assert tailored_output_dir("ana-fandino", multiple=True, output_dir="custom") == "custom/ana-fandino"


def test_person_runs_follow_generated_manifests(tmp_path: Path):
    manifest = tmp_path / "output" / "people" / "ana-fandino" / "tailored_cvs" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    runs = person_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].slug == "ana-fandino"
    assert runs[0].log_dir == tmp_path / "output" / "people" / "ana-fandino" / "logs"
    assert runs[0].browser_profile_dir == tmp_path / "output" / "people" / "ana-fandino" / "browser_profile"


def test_manifest_keeps_the_person(tmp_path: Path):
    role = ManifestRole(
        title="Backend Engineer",
        slug="backend-engineer",
        pdf_path="output/people/ana-fandino/tailored_cvs/backend-engineer_cv.pdf",
        html_path="output/people/ana-fandino/tailored_cvs/backend-engineer_cv.html",
        metadata_path="output/people/ana-fandino/tailored_cvs/backend-engineer_cv.json",
    )
    path = tmp_path / "manifest.json"
    applicant = Applicant(full_name="Ana Example", email="ana@example.com")
    merge_manifest(
        path,
        source_cv="input/Ana_Example_CV.pdf",
        provider="openai",
        model="gpt-test",
        role=role,
        person_slug="ana-fandino",
        applicant=applicant,
    )
    loaded = load_manifest(path)
    assert loaded.person_slug == "ana-fandino"
    assert loaded.applicant.email == "ana@example.com"
    assert loaded.roles[0].slug == "backend-engineer"


def test_settings_for_person_only_changes_the_browser_profile(tmp_path: Path):
    settings = Settings(_env_file=None)
    updated = settings_for_person(
        settings,
        Applicant(full_name="Camila Example", email="camila@example.com"),
        tmp_path / "browser",
    )
    assert updated.browser_profile_dir == str(tmp_path / "browser")
    assert not hasattr(updated, "full_name")
    assert not hasattr(updated, "email")
