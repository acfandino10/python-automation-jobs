import pytest

from src.config import Settings
from src.errors import ConfigError
from src.job_applier import (
    canonical_url,
    is_agreement_question,
    is_eeo_question,
    keyword_match,
    needs_human,
    profile_value_for_label,
    yes_no_answer,
)
from src.llm import parse_json_object
from src.models import Applicant


def test_parse_json_object_accepts_a_fence():
    payload = parse_json_object('```json\n{"score": 81, "summary": "Strong fit"}\n```')
    assert payload["score"] == 81


def test_canonical_url_keeps_the_job_id_and_drops_tracking():
    raw = "https://www.indeed.com/viewjob?jk=abc123&utm_source=google&from=serp"
    assert canonical_url(raw) == "https://www.indeed.com/viewjob?jk=abc123"


def test_keyword_match_uses_real_terms():
    assert keyword_match("Backend Engineer", "We use Python and SQL.", ["Python"], "Backend Engineer")
    assert not keyword_match("Retail Associate", "Fold shirts.", ["Python", "SQL"], "Backend Engineer")


def test_profile_values_and_work_authorization():
    applicant = Applicant(
        full_name="Jordan Lee",
        email="jordan@example.com",
        phone="+1 555 0100",
        linkedin_url="https://linkedin.com/in/jordan",
        portfolio_url="https://jordan.example",
        location="Remote",
        work_authorization="Authorized to work in Colombia and does not require sponsorship.",
    )
    assert profile_value_for_label("First Name", applicant) == "Jordan"
    assert profile_value_for_label("Last Name", applicant) == "Lee"
    assert profile_value_for_label("Email address", applicant) == "jordan@example.com"
    assert profile_value_for_label("LinkedIn URL", applicant) == "https://linkedin.com/in/jordan"
    assert yes_no_answer("Will you now or in the future require sponsorship?", applicant.work_authorization) == "No"
    assert yes_no_answer("Are you authorized to work in the United States?", applicant.work_authorization) is None
    assert yes_no_answer("Are you legally authorized to work?", applicant.work_authorization) == "Yes"


def test_human_and_sensitive_questions():
    assert needs_human("https://www.indeed.com/jobs", "Please verify you are human")
    assert needs_human("https://www.linkedin.com/login", "Welcome")
    assert needs_human("https://example.com/jobs/1", "Python role") is None
    assert is_eeo_question("Gender (voluntary)")
    assert is_agreement_question("I agree to the privacy policy")
    assert not is_eeo_question("Years of Python experience")


def test_unknown_board_is_rejected():
    settings = Settings(search_boards="monster")
    with pytest.raises(ConfigError):
        settings.board_list()
