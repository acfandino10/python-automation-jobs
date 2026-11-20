import json
from pathlib import Path

import pytest

import apply_jobs
from src.config import Settings
from src.errors import ConfigError
from src.job_applier import (
    LINKEDIN_NEXT_SELECTORS,
    _accept_easy_apply,
    _accept_next,
    _accept_submit,
    assign_form_tags,
    canonical_url,
    form_tag_requests,
    found_contact_tags,
    missing_contact_tags,
    run_control_recovery_agent,
    run_form_tag_agent,
    is_agreement_question,
    is_eeo_question,
    keyword_match,
    needs_human,
    parse_field_tags,
    profile_value_for_field,
    profile_value_for_label,
    value_for_tag,
    yes_no_answer,
)
from src.llm import parse_json_object
from src.models import Applicant, Manifest, ManifestRole


def test_school_comes_from_the_first_education_entry(tmp_path: Path):
    from src.job_applier import _school_from_role

    metadata = tmp_path / "role.json"
    metadata.write_text(
        json.dumps(
            {
                "target_role": "Engineer",
                "city": "Bogotá",
                "country": "Colombia",
                "education": [
                    {"degree": "B.S. in Systems Engineering", "school": "Universidad de los Andes, Colombia", "dates": "2012 – 2017"},
                    {"degree": "Research", "school": "University of Wisconsin-Milwaukee (UWM), USA", "dates": "2016"},
                ],
            }
        ),
        encoding="utf-8",
    )
    role = ManifestRole(
        title="Engineer",
        slug="engineer",
        pdf_path="role.pdf",
        html_path="role.html",
        metadata_path=str(metadata),
    )
    from src.job_applier import _role_facts, _with_role_facts

    facts = _role_facts(role, Settings())
    assert facts.school == "Universidad de los Andes, Colombia"
    assert facts.degree == "B.S. in Systems Engineering"
    assert facts.city == "Bogotá"
    assert facts.country == "Colombia"
    filled = _with_role_facts(Applicant(full_name="Ana Fandiño", location="Remote"), facts)
    assert filled.location == "Bogotá, Colombia"
    assert filled.school == "Universidad de los Andes, Colombia"
    assert filled.degree == "B.S. in Systems Engineering"
    assert _school_from_role(role, Settings()) == "Universidad de los Andes, Colombia"


def test_stripe_fields_use_the_city_and_the_first_education_entry():
    from src.job_applier import JobApplier

    applicant = Applicant(
        full_name="Ana Fandiño",
        location="Bogotá, Colombia",
        school="Universidad de los Andes, Colombia",
        degree="B.S. in Systems Engineering",
    )
    city = _empty_field("Location (City)*", "input:text")
    school = _empty_field("School*", "input:text")
    country = _empty_field("Please select the country where you currently reside. *", "select")
    degree = _empty_field("Degree", "select")
    country["options"] = ["United States", "Colombia", "Canada"]
    degree["options"] = ["High School", "Bachelor's Degree", "Master's Degree"]
    for field in (city, school, country, degree):
        field["handle"] = _MemoryHandle()
        field["tag"] = "screening"
        JobApplier._fill_tagged(object(), field, "screening", applicant, "CV", [], 0)

    assert city["handle"].value == "Bogotá"
    assert school["handle"].value == "Universidad de los Andes, Colombia"
    assert country["handle"].value == "Colombia"
    assert degree["handle"].value == "Bachelor's Degree"


def test_name_ids_are_filled_from_the_split_name():
    from src.job_applier import _fill_name_ids

    class _Handle:
        def __init__(self) -> None:
            self.value = ""

        def is_visible(self) -> bool:
            return True

        def evaluate(self, script: str, arg: str | None = None) -> str | None:
            if arg is not None:
                self.value = arg
                return None
            return "INPUT"

        def input_value(self, timeout: int = 0) -> str:
            return self.value

        def get_attribute(self, name: str) -> None:
            return None

        def fill(self, text: str, timeout: int = 0) -> None:
            self.value = text

        def click(self, timeout: int = 0) -> None:
            return None

    class _Loc:
        def __init__(self, handles: list[_Handle]) -> None:
            self._handles = handles

        def count(self) -> int:
            return len(self._handles)

        def nth(self, index: int) -> _Handle:
            return self._handles[index]

    fields = {name: _Handle() for name in ("first_name", "last_name", "full_name")}

    class _Scope:
        def locator(self, selector: str) -> _Loc:
            if "application--questions" in selector:
                return _Loc([])
            for field_id, handle in fields.items():
                if selector == f'[id="{field_id}"]':
                    return _Loc([handle])
            return _Loc([])

    _fill_name_ids(_Scope(), Applicant(full_name="Ana Fandiño"))
    assert fields["first_name"].value == "Ana"
    assert fields["last_name"].value == "Fandiño"
    assert fields["full_name"].value == "Ana Fandiño"
    explicit = Applicant(full_name="Ana Fandiño", first_name="Ana Camila", last_name="Fandiño")
    assert explicit.first_name == "Ana Camila"
    assert explicit.last_name == "Fandiño"
    assert explicit.full_name == "Ana Fandiño"


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
    placed = Applicant(full_name="Ana Example", location="Bogotá, Colombia")
    assert profile_value_for_label("First Name*", placed) == "Ana"
    assert profile_value_for_label("Country*", placed) == "Colombia"
    assert profile_value_for_label("Location (City)*", placed) == "Bogotá"
    assert profile_value_for_label("Please select the country where you currently reside", placed) == "Colombia"
    degreed = placed.model_copy(update={"degree": "B.S. in Systems Engineering", "school": "Universidad de los Andes, Colombia"})
    assert profile_value_for_label("School*", degreed) == "Universidad de los Andes, Colombia"
    assert profile_value_for_label("Degree", degreed) == "B.S. in Systems Engineering"
    prefixed = Applicant(phone="+57 319 482 6159", location="Bogotá, Colombia")
    assert profile_value_for_label("Country code", prefixed) == "+57"
    assert profile_value_for_label("Phone", prefixed) == "319 482 6159"
    assert value_for_tag("phone_prefix", prefixed) == "+57"
    assert value_for_tag("phone", prefixed) == "319 482 6159"
    assert value_for_tag("city", prefixed) == "Bogotá"
    assert yes_no_answer("Will you now or in the future require sponsorship?", applicant.work_authorization) == "No"
    assert yes_no_answer("Are you authorized to work in the United States?", applicant.work_authorization) is None
    assert yes_no_answer("Are you legally authorized to work?", applicant.work_authorization) == "Yes"


def test_the_field_before_phone_is_the_calling_code():
    from src.job_applier import _phone_prefix_option, mark_phone_prefix_before_phone

    email = _empty_field("Email", "input:email", name="email")
    email["tag"] = "email"
    country = _empty_field("Country", "select", name="country")
    country["tag"] = "country"
    country["options"] = ["United States (+1)", "Colombia (+57)"]
    phone = _empty_field("Phone", "input:tel", name="phone")
    phone["tag"] = "phone"
    blank = _empty_field("Field 4", "input:text", name="code")
    mobile = _empty_field("Mobile", "input:tel", name="mobile")

    mark_phone_prefix_before_phone([email, country, phone, blank, mobile])

    assert email["tag"] == "email"
    assert country["tag"] == "phone_prefix"
    assert phone["tag"] == "phone"
    assert blank["tag"] == "phone_prefix"
    assert _phone_prefix_option(country["options"], "+57", "Colombia") == "Colombia (+57)"
    assert _phone_prefix_option(["Colombia", "United States"], "+57", "Colombia") == "Colombia"


def test_linkedin_easy_apply_button_labels():
    assert _accept_easy_apply("Easy Apply")
    assert _accept_easy_apply("Easy Apply to SDE at Acme")
    assert not _accept_easy_apply("Apply")
    assert _accept_next("Next")
    assert _accept_next("Continue")
    assert _accept_next("Continue to next step")
    assert _accept_next("Review your application")
    assert not _accept_next("Next page")
    assert not _accept_next("Back")
    assert not _accept_next("Submit application")
    assert not _accept_next("")
    assert _accept_submit("Submit application")
    assert not _accept_submit("Next")
    assert not _accept_submit("Submitted")
    assert not _accept_submit("")
    joined = " ".join(LINKEDIN_NEXT_SELECTORS)
    assert "Next" in joined
    assert "Continue to next step" in joined


def test_human_and_sensitive_questions():
    assert needs_human("https://www.indeed.com/jobs", "Please verify you are human")
    assert needs_human("https://smartapply.indeed.com/form", "Are you a human?")
    assert needs_human("https://www.indeed.com/jobs", "Before you continue, verify you are human")
    assert needs_human("https://www.linkedin.com/login", "Welcome")
    assert needs_human("https://example.com/jobs/1", "Python role") is None
    assert is_eeo_question("Gender (voluntary)")
    assert is_agreement_question("I agree to the privacy policy")
    assert not is_eeo_question("Years of Python experience")


def test_people_query_selects_one_several_or_all(tmp_path: Path):
    runs = [
        _person_run(tmp_path, "ana-fandino", "Ana Example"),
        _person_run(tmp_path, "camila-fandino", "Camila Example"),
    ]

    only_ana = apply_jobs._people_from_query(runs, "ana-fandino")
    assert [run.slug for run in only_ana] == ["ana-fandino"]

    by_name = apply_jobs._people_from_query(runs, "Camila Example")
    assert [run.slug for run in by_name] == ["camila-fandino"]

    both = apply_jobs._people_from_query(runs, "ana-fandino, camila-fandino")
    assert [run.slug for run in both] == ["ana-fandino", "camila-fandino"]

    everyone = apply_jobs._people_from_query(runs, "all")
    assert [run.slug for run in everyone] == ["ana-fandino", "camila-fandino"]

    with pytest.raises(ConfigError, match="Unknown person"):
        apply_jobs._people_from_query(runs, "someone-else")

    assert [run.slug for run in apply_jobs._runs_for_selection(runs, ["ana-fandino"])] == ["ana-fandino"]
    assert [run.slug for run in apply_jobs._runs_for_selection(runs, ["all"])] == [
        "ana-fandino",
        "camila-fandino",
    ]
    assert [run.slug for run in apply_jobs._runs_for_selection(runs, ["all", "ana-fandino"])] == [
        "ana-fandino",
        "camila-fandino",
    ]


def _person_run(root: Path, slug: str, full_name: str):
    manifest = root / "output" / "people" / slug / "tailored_cvs" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "generated_at": "2026-11-19T00:00:00+00:00",
                "source_cv": f"input/{slug}.pdf",
                "provider": "openai",
                "model": "gpt-test",
                "person_slug": slug,
                "applicant": {"full_name": full_name},
                "roles": [],
            }
        ),
        encoding="utf-8",
    )
    base = root / "output" / "people" / slug
    return apply_jobs.PersonRun(
        slug=slug,
        manifest_path=manifest,
        log_dir=base / "logs",
        browser_profile_dir=base / "browser_profile",
    )


def test_role_checkbox_keeps_one_several_or_all():
    manifest = Manifest(
        generated_at="2026-11-19T00:00:00+00:00",
        source_cv="input/example.pdf",
        provider="openai",
        model="gpt-test",
        roles=[
            _role("Lead Full Stack Engineer", "lead-full-stack-engineer"),
            _role("Senior React Developer", "senior-react-developer"),
        ],
    )

    one = apply_jobs._roles_for_selection(manifest, ["lead-full-stack-engineer"])
    assert [role.slug for role in one] == ["lead-full-stack-engineer"]

    several = apply_jobs._roles_for_selection(
        manifest,
        ["lead-full-stack-engineer", "senior-react-developer"],
    )
    assert [role.slug for role in several] == ["lead-full-stack-engineer", "senior-react-developer"]

    everyone = apply_jobs._roles_for_selection(manifest, ["all", "lead-full-stack-engineer"])
    assert [role.slug for role in everyone] == ["lead-full-stack-engineer", "senior-react-developer"]
    assert [role.slug for role in apply_jobs._roles_from_query(manifest, "all")] == [
        "lead-full-stack-engineer",
        "senior-react-developer",
    ]


def _role(title: str, slug: str) -> ManifestRole:
    return ManifestRole(
        title=title,
        slug=slug,
        pdf_path=f"{slug}.pdf",
        html_path=f"{slug}.html",
        metadata_path=f"{slug}.json",
    )


def test_enter_on_a_highlighted_row_selects_that_row():
    import questionary
    from prompt_toolkit.keys import Keys

    question = questionary.checkbox(
        "Which people should I apply for?",
        choices=[
            questionary.Choice("All", value="all"),
            questionary.Choice("Ana Example (ana-fandino)", value="ana-fandino"),
            questionary.Choice("Camila Example (camila-fandino)", value="camila-fandino"),
        ],
    )
    apply_jobs._install_enter_selects_highlighted(question)
    control = apply_jobs._inquirer_control(question.application)
    control.pointed_at = 1
    handler = next(
        binding.handler
        for binding in question.application.key_bindings.bindings
        if binding.keys == (Keys.ControlM,)
    )

    class _App:
        def exit(self, result=None, **_kwargs):
            self.result = result

    event = type("Event", (), {"app": _App()})()
    handler(event)
    assert event.app.result == ["ana-fandino"]


def test_board_checkbox_keeps_one_several_or_all():
    assert apply_jobs._boards_for_selection(["indeed"]) == ["indeed"]
    assert apply_jobs._boards_for_selection(["greenhouse", "linkedin"]) == ["linkedin", "greenhouse"]
    assert apply_jobs._boards_for_selection(["all", "indeed"]) == ["indeed", "linkedin", "greenhouse"]
    with pytest.raises(ConfigError, match="at least one job board"):
        apply_jobs._boards_for_selection([])


def test_stuck_button_waits_five_minutes():
    from src.job_applier import JOB_LIMIT_SECONDS, _read_stdin, _seconds_remaining

    assert JOB_LIMIT_SECONDS == 300
    assert _seconds_remaining(100.0, 40.0) == 60
    assert _seconds_remaining(100.0, 100.0) == 0
    assert _read_stdin(0) is None


def test_unknown_board_is_rejected():
    settings = Settings(search_boards="monster")
    with pytest.raises(ConfigError):
        settings.board_list()


class _EmptyHandle:
    def input_value(self, timeout=400):
        return ""

    def is_checked(self):
        return False

    def get_attribute(self, name):
        return None


def _empty_field(label: str, field_type: str, name: str = "field") -> dict:
    return {
        "handle": _EmptyHandle(),
        "label": label,
        "group_label": "",
        "choice": "",
        "name": name,
        "required": True,
        "type": field_type,
        "options": [],
        "context": label,
    }


def test_model_tag_fills_the_control_not_the_misleading_label():
    from rich.console import Console

    from src.job_applier import JobApplier

    applicant = Applicant(
        full_name="Ana Example",
        email="ana@example.com",
        phone="+57 300 000 0000",
        location="Bogotá, Colombia",
    )
    phone = _empty_field("Years of experience with email marketing", "input:text", name="phone")
    upload = _empty_field("Resume/CV", "input:file", name="resume")
    others = [phone, upload]

    class _Model:
        def __init__(self):
            self.user = ""

        def complete_json(self, system, user, **kwargs):
            self.user = user
            return {
                "fields": [
                    {"id": 0, "tag": "phone"},
                    {"id": 1, "tag": "not-a-field"},
                    {"id": 9, "tag": "email"},
                ]
            }

    box = type("Box", (), {})()
    box.llm = _Model()
    box.console = Console(quiet=True)
    JobApplier._identify_form_tags(box, {}, others)

    assert "email marketing" in box.llm.user
    assert "input:file" not in box.llm.user
    assert phone["tag"] == "phone"
    assert "tag" not in upload
    assert profile_value_for_field(phone["tag"], phone["label"], applicant) == "300 000 0000"
    assert profile_value_for_label(phone["label"], applicant) is None
    assert profile_value_for_field("skip", "Email address", applicant) is None
    assert profile_value_for_field(None, "Email address", applicant) == "ana@example.com"
    assert profile_value_for_label("Email address", applicant) == "ana@example.com"
    schooled = Applicant(full_name="Ana Example", school="Universidad de los Andes, Colombia")
    assert profile_value_for_label("School", schooled) == "Universidad de los Andes, Colombia"
    assert profile_value_for_label("University", schooled) == "Universidad de los Andes, Colombia"
    assert profile_value_for_label("School Name*", schooled) == "Universidad de los Andes, Colombia"
    assert profile_value_for_label("Highest level of education", schooled) is None
    assert profile_value_for_label("Did you graduate high school?", schooled) is None
    assert profile_value_for_label("first_name", Applicant(full_name="Ana Example")) == "Ana"
    assert value_for_tag("school", schooled) == "Universidad de los Andes, Colombia"


def test_parse_field_tags_drops_unknown_ids_and_tags():
    tags = parse_field_tags(
        {"fields": [{"id": "0", "tag": "Work Authorization"}, {"index": 3, "tag": "city"}, {"id": 1, "tag": "nope"}]},
        {0, 1},
    )
    assert tags == {0: "work_authorization"}


def test_form_tag_requests_keep_radio_groups_and_skip_files():
    yes = _empty_field("Yes", "input:radio", name="auth")
    yes["choice"] = "Yes"
    yes["group_label"] = "Are you authorized to work?"
    no = _empty_field("No", "input:radio", name="auth")
    no["choice"] = "No"
    no["group_label"] = "Are you authorized to work?"
    email = _empty_field("Email", "input:email", name="email")
    upload = _empty_field("Resume", "input:file", name="resume")
    rows = form_tag_requests({"auth": [yes, no]}, [email, upload])
    assert [row["kind"] for row in rows] == ["radio", "field"]
    assert rows[0]["choices"] == ["Yes", "No"]
    assert rows[1]["name"] == "email"
    tags = parse_field_tags({"fields": [{"id": 0, "tag": "work_authorization"}, {"id": 1, "tag": "email"}]}, {0, 1})
    assign_form_tags({"auth": [yes, no]}, [email, upload], tags, rows)
    assert yes["tag"] == "work_authorization"
    assert no["tag"] == "work_authorization"
    assert email["tag"] == "email"


def test_form_agent_corrects_a_contact_tag_on_a_radio():
    yes = _empty_field("Yes", "input:radio", name="auth")
    yes["choice"] = "Yes"
    yes["group_label"] = "Are you authorized to work?"
    no = _empty_field("No", "input:radio", name="auth")
    no["choice"] = "No"
    phone = _empty_field("Phone", "input:tel", name="phone")
    rows = form_tag_requests({"auth": [yes, no]}, [phone])

    class _Model:
        def __init__(self):
            self.calls = 0

        def complete_json(self, system, user, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"fields": [{"id": 0, "tag": "email"}, {"id": 1, "tag": "phone"}]}
            assert "do not fit" in user
            return {"fields": [{"id": 0, "tag": "work_authorization"}, {"id": 1, "tag": "phone"}]}

    model = _Model()
    assert run_form_tag_agent(model, rows) == {0: "work_authorization", 1: "phone"}
    assert model.calls == 2


def test_form_agent_drops_a_tag_that_stays_wrong():
    yes = _empty_field("Yes", "input:radio", name="auth")
    yes["group_label"] = "Work authorization"
    rows = form_tag_requests({"auth": [yes]}, [])

    class _Model:
        def complete_json(self, system, user, **kwargs):
            return {"fields": [{"id": 0, "tag": "email"}]}

    assert run_form_tag_agent(_Model(), rows) == {}


def test_form_agent_failure_leaves_the_label_matcher():
    from rich.console import Console

    from src.errors import LLMError
    from src.job_applier import JobApplier

    applicant = Applicant(email="ana@example.com")
    email = _empty_field("Email address", "input:text", name="email")

    class _Model:
        def complete_json(self, system, user, **kwargs):
            raise LLMError("down")

    box = type("Box", (), {})()
    box.llm = _Model()
    box.console = Console(quiet=True)
    JobApplier._identify_form_tags(box, {}, [email])
    assert "tag" not in email
    assert profile_value_for_label(email["label"], applicant) == "ana@example.com"


def test_missing_contact_fields_ignore_a_labeled_email():
    unlabeled = {"tag": "skip", "label": "Email", "type": "input:text", "group_label": ""}
    labeled = {"tag": "", "label": "Email address", "type": "input:email", "group_label": ""}
    applicant = Applicant(full_name="Ana Example", email="ana@example.com", phone="+57 300")
    missing = missing_contact_tags(found_contact_tags([unlabeled]), applicant)
    assert "email" in missing
    assert "phone" in missing
    assert "first_name" in missing
    found = missing_contact_tags(found_contact_tags([labeled]), applicant)
    assert "email" not in found


def test_recovery_agent_keeps_only_the_missing_tag():
    contact = _empty_field("Contact", "role:combobox", name="custom-email")
    notes = _empty_field("Notes", "input:text", name="notes")
    from src.job_applier import recovery_requests

    rows = recovery_requests({}, [contact, notes], [])

    class _Model:
        def complete_json(self, system, user, **kwargs):
            assert "email" in user
            return {"fields": [{"id": 0, "tag": "email"}, {"id": 1, "tag": "phone"}]}

    assert run_control_recovery_agent(_Model(), rows, ["email", "first_name"]) == {0: "email"}


def test_recovery_agent_picks_a_control_the_first_pass_missed():
    from rich.console import Console

    from src.job_applier import JobApplier

    contact = _empty_field("Contact", "role:combobox", name="custom-email")
    applicant = Applicant(full_name="Ana Example", email="ana@example.com", phone="+57 300")

    class _Model:
        def complete_json(self, system, user, **kwargs):
            assert "missing" in user
            return {"fields": [{"id": 0, "tag": "email"}]}

    class _NoPage:
        def locator(self, selector):
            raise RuntimeError("no browser")

    box = type("Box", (), {})()
    box.llm = _Model()
    box.console = Console(quiet=True)
    others = [contact]
    JobApplier._recover_missing_controls(box, _NoPage(), _NoPage(), {}, others, applicant)
    assert contact["tag"] == "email"


def test_recovery_does_not_run_when_the_contact_controls_exist():
    from rich.console import Console

    from src.job_applier import JobApplier

    fields = []
    for label, tag, field_type in (
        ("First name", "first_name", "input:text"),
        ("Last name", "last_name", "input:text"),
        ("Email", "email", "input:email"),
        ("Phone", "phone", "input:tel"),
    ):
        field = _empty_field(label, field_type, name=tag)
        field["tag"] = tag
        fields.append(field)

    class _Model:
        def complete_json(self, system, user, **kwargs):
            raise AssertionError("the recovery agent should not run")

    class _NoPage:
        def locator(self, selector):
            raise RuntimeError("no browser")

    box = type("Box", (), {})()
    box.llm = _Model()
    box.console = Console(quiet=True)
    JobApplier._recover_missing_controls(
        box,
        _NoPage(),
        _NoPage(),
        {},
        fields,
        Applicant(full_name="Ana Example", email="ana@example.com", phone="+57 300"),
    )


class _MemoryHandle:
    def __init__(self, value: str = ""):
        self.value = value
        self.checked = False

    def input_value(self, timeout=400):
        return self.value

    def is_checked(self):
        return self.checked

    def get_attribute(self, name):
        return None

    def fill(self, text, timeout=1500):
        self.value = text

    def click(self, timeout=1000):
        return None

    def check(self, timeout=1000):
        self.checked = True

    def press(self, key):
        return None

    def select_option(self, label=None, value=None):
        self.value = label or value or ""
        return [self.value]

    def evaluate(self, script, *args):
        if "selectedIndex" in str(script):
            return self.value
        raise RuntimeError("no browser")


def test_empty_fields_are_sent_for_profile_inference():
    from src.job_applier import infer_field_requests

    company = _empty_field("Current company", "input:text", name="company")
    company["required"] = False
    company["handle"] = _MemoryHandle()
    degree = _empty_field("Degree", "select", name="degree")
    degree["required"] = True
    degree["options"] = ["B.S.", "M.S."]
    degree["handle"] = _MemoryHandle()
    gender = _empty_field("Gender", "select", name="gender")
    gender["tag"] = "eeo"
    gender["handle"] = _MemoryHandle()
    email = _empty_field("Email", "input:email", name="email")
    email["handle"] = _MemoryHandle("ana@example.com")
    agree = _empty_field("I agree to the terms of service", "input:checkbox", name="agree")
    agree["handle"] = _MemoryHandle()
    upload = _empty_field("Resume", "input:file", name="resume")
    follow = _empty_field("Follow company", "input:checkbox", name="follow")
    follow["required"] = False
    follow["handle"] = _MemoryHandle()

    rows = infer_field_requests({}, [company, degree, gender, email, agree, upload, follow])
    assert [row["label"] for row in rows] == ["Degree", "Current company"]
    assert rows[0]["options"] == ["B.S.", "M.S."]


def test_inferred_answers_keep_only_known_ids():
    from src.job_applier import parse_inferred_answers

    answers = parse_inferred_answers(
        {"fields": [{"id": "0", "answer": "The Home Depot"}, {"id": 1, "answer": "  "}, {"id": 9, "answer": "nope"}]},
        {0, 1},
    )
    assert answers == {0: "The Home Depot"}


def test_infer_agent_corrects_an_answer_outside_the_options():
    from src.job_applier import infer_field_requests, run_profile_infer_agent

    degree = _empty_field("Degree", "select", name="degree")
    degree["options"] = ["B.S.", "M.S."]
    degree["handle"] = _MemoryHandle()
    rows = infer_field_requests({}, [degree])

    class _Model:
        def __init__(self):
            self.calls = 0

        def complete_json(self, system, user, **kwargs):
            self.calls += 1
            assert "Ana Example" in user
            if self.calls == 1:
                return {"fields": [{"id": 0, "answer": "Bachelor"}]}
            assert "not one of the options" in user
            return {"fields": [{"id": 0, "answer": "B.S."}]}

    model = _Model()
    assert run_profile_infer_agent(model, rows, "Name: Ana Example", "B.S. in Systems Engineering") == {0: "B.S."}
    assert model.calls == 2


def test_infer_agent_drops_an_answer_that_stays_outside_the_options():
    from src.job_applier import infer_field_requests, run_profile_infer_agent

    degree = _empty_field("Degree", "select", name="degree")
    degree["options"] = ["B.S.", "M.S."]
    degree["handle"] = _MemoryHandle()
    rows = infer_field_requests({}, [degree])

    class _Model:
        def complete_json(self, system, user, **kwargs):
            return {"fields": [{"id": 0, "answer": "Bachelor"}]}

    assert run_profile_infer_agent(_Model(), rows, "Name: Ana Example", "B.S.") == {}


def test_inferred_answers_are_written_and_clear_the_pause():
    from src.job_applier import apply_inferred_answers, infer_field_requests, _without_filled

    company = _empty_field("Current company", "input:text", name="company")
    company["handle"] = _MemoryHandle()
    degree = _empty_field("Degree", "select", name="degree")
    degree["options"] = ["B.S.", "M.S."]
    degree["handle"] = _MemoryHandle()
    yes = _empty_field("Yes", "input:radio", name="remote")
    yes["choice"] = "Yes"
    yes["group_label"] = "Willing to work remotely?"
    yes["handle"] = _MemoryHandle()
    no = _empty_field("No", "input:radio", name="remote")
    no["choice"] = "No"
    no["group_label"] = "Willing to work remotely?"
    no["handle"] = _MemoryHandle()
    radios = {"remote": [yes, no]}
    rows = infer_field_requests(radios, [company, degree])
    answers = {row["id"]: {"Current company": "The Home Depot", "Degree": "B.S.", "Willing to work remotely?": "Yes"}[row["label"]] for row in rows}

    filled = apply_inferred_answers(radios, rows, answers)
    assert company["handle"].value == "The Home Depot"
    assert degree["handle"].value == "B.S."
    assert yes["handle"].checked is True
    assert filled == {"Current company", "Degree", "Willing to work remotely?"}
    unresolved = _without_filled(
        ["Current company", "Salary was not found in the CV: Degree", "Voluntary question left blank: Gender"],
        filled,
    )
    assert unresolved == ["Voluntary question left blank: Gender"]


def test_empty_fields_are_inferred_from_the_profile():
    from rich.console import Console

    from src.errors import LLMError
    from src.job_applier import JobApplier

    company = _empty_field("Current company", "input:text", name="company")
    company["required"] = False
    company["handle"] = _MemoryHandle()

    class _Model:
        def complete_json(self, system, user, **kwargs):
            assert "The Home Depot" in user
            assert "Current company" in user
            return {"fields": [{"id": 0, "answer": "The Home Depot"}]}

    box = type("Box", (), {})()
    box.llm = _Model()
    box.console = Console(quiet=True)
    filled = JobApplier._infer_empty_fields(
        box,
        {},
        [company],
        Applicant(full_name="Ana Example"),
        "Lead Full Stack Engineer — The Home Depot",
    )
    assert filled == {"Current company"}
    assert company["handle"].value == "The Home Depot"

    class _Down:
        def complete_json(self, system, user, **kwargs):
            raise LLMError("down")

    other = _empty_field("Degree", "input:text", name="degree")
    other["handle"] = _MemoryHandle()
    box.llm = _Down()
    assert JobApplier._infer_empty_fields(box, {}, [other], Applicant(full_name="Ana Example"), "B.S.") == set()
    assert other["handle"].value == ""
