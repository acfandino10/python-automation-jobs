"""Environment-backed settings for the tailor and the application runner."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_MODELS = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-5-5",
    "gemini": "gemini-3.8-flash",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm_provider: Literal["openai", "anthropic", "gemini"] = "openai"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    llm_model: str = ""

    job_location: str = "Remote"
    remote_only: bool = True
    match_threshold: int = Field(default=75, ge=0, le=100)
    search_boards: str = "indeed,linkedin"
    greenhouse_boards: str = ""

    browser_profile_dir: str = "output/browser_profile"
    headless: bool = False
    min_delay_seconds: float = 0.8
    max_delay_seconds: float = 2.2
    min_between_applications: float = 6.0
    max_between_applications: float = 14.0
    pdf_engine: Literal["auto", "weasyprint", "reportlab"] = "auto"

    @field_validator("llm_provider", "pdf_engine", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return self.project_root / candidate

    def api_key(self) -> str:
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
                f"{env_name} is empty. Copy .env.example to .env and set the key for LLM_PROVIDER={self.llm_provider}."
            )
        return key

    def resolved_model(self) -> str:
        if self.llm_model.strip():
            return self.llm_model.strip()
        return _DEFAULT_MODELS[self.llm_provider]

    def board_list(self) -> list[str]:
        allowed = {"indeed", "linkedin", "greenhouse"}
        boards = []
        for raw in self.search_boards.split(","):
            name = raw.strip().lower()
            if not name:
                continue
            if name not in allowed:
                raise ConfigError(f"Unknown search board '{name}'. Use indeed, linkedin, or greenhouse.")
            if name not in boards:
                boards.append(name)
        if not boards:
            raise ConfigError("SEARCH_BOARDS is empty. Set it to indeed, linkedin, greenhouse, or a combination.")
        return boards

    def greenhouse_board_tokens(self) -> list[str]:
        tokens = []
        for raw in self.greenhouse_boards.split(","):
            token = raw.strip().strip("/")
            if token and token not in tokens:
                tokens.append(token)
        return tokens


def load_settings() -> Settings:
    return Settings()
