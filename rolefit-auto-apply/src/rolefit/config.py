"""Environment settings and the YAML candidate profile."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rolefit.exceptions import ConfigError
from rolefit.models import Applicant

PdfEngine = Literal["auto", "weasyprint", "reportlab"]
LLMProvider = Literal["openai", "anthropic", "gemini"]

_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.5-flash",
}

def project_root() -> Path:
    """Return the repository root when running from a source checkout.

    Installed copies fall back to the current working directory, which is where
    ``profile.yaml`` and ``output/`` are expected.
    """
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return Path.cwd()


class WorkPreferences(BaseModel):
    """Search defaults that are safe to commit in an example profile."""

    model_config = ConfigDict(extra="ignore")

    job_location: str = "Remote"
    remote_only: bool = True
    match_threshold: int = Field(default=75, ge=0, le=100)
    search_boards: list[str] = Field(default_factory=lambda: ["greenhouse"])
    greenhouse_boards: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    """Non-secret candidate facts loaded from ``profile.yaml``."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    full_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    location: str = ""
    work_authorization: str = ""
    salary_expectation: str = ""
    preferences: WorkPreferences = Field(default_factory=WorkPreferences)

    def applicant(self) -> Applicant:
        """Return the contact record used by the tailor and form filler."""
        return Applicant(
            full_name=self.full_name,
            email=self.email,
            phone=self.phone,
            linkedin_url=self.linkedin_url,
            portfolio_url=self.portfolio_url,
            location=self.location or self.preferences.job_location,
            work_authorization=self.work_authorization,
            salary_expectation=self.salary_expectation,
        )


class Settings(BaseSettings):
    """Process configuration loaded from the environment and an optional ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm_provider: LLMProvider = "openai"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    llm_model: str = ""

    job_location: str = ""
    remote_only: bool | None = None
    match_threshold: int | None = Field(default=None, ge=0, le=100)
    search_boards: str = ""
    greenhouse_boards: str = ""

    browser_profile_dir: str = "output/browser_profile"
    headless: bool = False
    min_delay_seconds: float = 0.8
    max_delay_seconds: float = 2.2
    min_between_applications: float = 6.0
    max_between_applications: float = 14.0
    pdf_engine: PdfEngine = "auto"
    profile_path: str = "profile.yaml"

    @field_validator("llm_provider", "pdf_engine", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    def resolve(self, path: str | Path) -> Path:
        """Resolve a configured path against the working directory."""
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return Path.cwd() / candidate

    def api_key(self) -> str:
        """Return the API key for the selected provider.

        Raises:
            ConfigError: The key for ``llm_provider`` is empty.
        """
        keys = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
        }
        key = keys[self.llm_provider].strip()
        if not key:
            env_name = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "gemini": "GEMINI_API_KEY",
            }[self.llm_provider]
            raise ConfigError(
                f"{env_name} is empty. Copy .env.example to .env and set the key "
                f"for LLM_PROVIDER={self.llm_provider}."
            )
        return key

    def resolved_model(self) -> str:
        """Return the configured model, or the provider default."""
        if self.llm_model.strip():
            return self.llm_model.strip()
        return _DEFAULT_MODELS[self.llm_provider]


def load_settings() -> Settings:
    """Load settings from the environment and a ``.env`` file in the working directory."""
    return Settings()


def load_profile(path: Path) -> CandidateProfile:
    """Load a candidate profile from YAML.

    A missing file yields an empty profile so environment overrides can still run.

    Args:
        path: Path to ``profile.yaml``.

    Raises:
        ConfigError: The file exists but is not a YAML mapping.
    """
    if not path.exists():
        return CandidateProfile()
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if loaded is None:
        return CandidateProfile()
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must be a YAML mapping of candidate fields.")
    return CandidateProfile.model_validate(loaded)


def merge_profile(settings: Settings, profile: CandidateProfile) -> CandidateProfile:
    """Overlay search preferences from the environment onto the YAML profile.

    Name, email, phone, and the other contact fields stay on the profile object
    only when ``profile.yaml`` contains them. The CLI does not use those fields.
    It fills forms from the CV. ``remote_only`` and ``match_threshold`` are
    overridden only when the environment set them.

    Args:
        settings: Environment-backed settings.
        profile: Profile loaded from YAML.

    Returns:
        The profile whose search preferences the applier should use.
    """
    data = profile.model_dump()
    preferences = dict(data["preferences"])
    if settings.job_location.strip():
        preferences["job_location"] = settings.job_location.strip()
    if settings.remote_only is not None:
        preferences["remote_only"] = settings.remote_only
    if settings.match_threshold is not None:
        preferences["match_threshold"] = settings.match_threshold
    if settings.search_boards.strip():
        preferences["search_boards"] = [
            part.strip().lower() for part in settings.search_boards.split(",") if part.strip()
        ]
    if settings.greenhouse_boards.strip():
        preferences["greenhouse_boards"] = [
            part.strip().strip("/")
            for part in settings.greenhouse_boards.split(",")
            if part.strip()
        ]
    data["preferences"] = preferences
    return CandidateProfile.model_validate(data)


def require_applicant(applicant: Applicant) -> Applicant:
    """Return the applicant after checking the fields an application form requires.

    Raises:
        ConfigError: The CV text did not include a name or an email address.
    """
    missing: list[str] = []
    if not applicant.full_name:
        missing.append("name")
    if not applicant.email:
        missing.append("email")
    if missing:
        raise ConfigError(
            "The CV is missing "
            + " and ".join(missing)
            + ". Add them to the résumé and run rolefit generate again."
        )
    return applicant
