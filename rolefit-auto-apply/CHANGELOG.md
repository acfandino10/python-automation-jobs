# Changelog

All notable changes to Rolefit are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-18

### Added

- `rolefit generate` parses a PDF or DOCX master CV, suggests roles, and writes one grounded PDF per role plus `manifest.json`.
- `rolefit apply` scores openings, uploads the matching role PDF, and logs every attempt to SQLite and CSV.
- `BaseJobBoard` plugin API with a Greenhouse adapter that uses the public jobs API and a persistent Playwright session.
- Provider-agnostic LLM client for OpenAI, Anthropic, and Gemini.
- Offline pytest suite, Ruff, strict Mypy, pre-commit, and GitHub Actions for Python 3.11 and 3.12.

[0.1.0]: https://github.com/rolefit/rolefit-auto-apply/releases/tag/v0.1.0
