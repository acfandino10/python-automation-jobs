"""User-facing errors for the CV tailor and application runner."""


class AutoApplyError(Exception):
    """Base error for failures the CLI can explain in plain language."""


class ConfigError(AutoApplyError):
    """Missing configuration, credentials, or runtime dependencies."""


class CVError(AutoApplyError):
    """The source CV could not be read or the tailored CV failed validation."""


class LLMError(AutoApplyError):
    """The language-model call failed or returned unusable output."""
