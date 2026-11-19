"""User-facing errors for the CV tailor and the application runner."""

from __future__ import annotations


class RolefitError(Exception):
    """Base error for failures the CLI can explain in plain language."""


class ConfigError(RolefitError):
    """Missing configuration, credentials, or an unknown job board."""


class CVError(RolefitError):
    """The source CV could not be read, or the tailored CV failed validation."""


class LLMError(RolefitError):
    """The language-model call failed or returned unusable output."""


class BoardError(RolefitError):
    """A job-board adapter could not search or fill an application."""
