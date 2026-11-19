# Rolefit

[![MIT License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](pyproject.toml)
[![CI](https://img.shields.io/badge/CI-ruff%20%7C%20mypy%20%7C%20pytest-2088FF.svg)](.github/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)

Rolefit turns one master CV into a grounded, role-specific PDF, then fills job applications with the PDF that matches that role.

<!-- Replace this line with ![Demo](docs/demo.gif) after you record a run. -->

The tailor may reorder bullets, rewrite the summary, and surface skills that already appear in the source CV. It drops employers, schools, projects, tools, and numbers the source does not contain. A live run types your profile into the form, uploads `{role_slug}_cv.pdf`, and stops for you on CAPTCHAs, sign-in walls, and any question it cannot answer from your profile or CV. It does not solve CAPTCHAs. Submit stays in your hands unless you pass `--submit`.

You are responsible for each site's terms, for the pace of a run, and for every answer that gets sent. Start with `--dry-run`.

## Architecture

```text
master CV  ->  parser  ->  CVData + raw text
                              |
                              v
                    LLM suggests roles (you confirm)
                              |
                              v
                    tailor grounds each role
                              |
                              v
              Jinja2 + WeasyPrint/ReportLab PDF
                              |
                              v
                    manifest.json + {slug}_cv.pdf
                              |
                              v
           board plugin searches  ->  LLM scores the description
                              |
                              v
              upload that role's PDF, log the attempt
```

Job boards are plugins. `BaseJobBoard` defines `search` and `apply`. The Greenhouse adapter is the reference implementation: public Job Board API for listings, Playwright persistent profile for the form. Add a board by subclassing `BaseJobBoard` and decorating it with `register_board`. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## Install

Python 3.11 or 3.12.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional extras:

```bash
pip install -e ".[llm,browser,pdf]"
playwright install chromium
```

`llm` installs the OpenAI, Anthropic, and Gemini clients. `browser` installs Playwright. `pdf` installs WeasyPrint, which also needs the system libraries from the [WeasyPrint docs](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html). `PDF_ENGINE=auto` uses ReportLab when WeasyPrint is not importable, so PDF generation works without those libraries.

```bash
cp .env.example .env
cp profile.example.yaml profile.yaml
```

Put API keys in `.env`. Put the board list and search preferences in `profile.yaml`. Name, email, phone, and links are taken from the CV, not from `.env`.

Copy a text-based PDF or DOCX to `input/base_cv.pdf`. Scanned image PDFs are rejected.

## Generate

```bash
rolefit generate --cv input/base_cv.pdf
```

The command suggests 5–7 titles, asks how many to keep, and asks you to confirm. Each confirmed role is written to:

- `output/tailored_cvs/{role-slug}_cv.pdf`
- `output/tailored_cvs/{role-slug}_cv.html`
- `output/tailored_cvs/{role-slug}_cv.json`
- `output/tailored_cvs/manifest.json`

Non-interactive:

```bash
rolefit generate \
  --cv input/base_cv.pdf \
  --roles "Senior Python Engineer,Data Engineer" \
  --yes \
  --pdf-engine reportlab
```

`--suggest-only` prints ideas and does not write PDFs.

`python scripts/generate_cvs.py --cv input/base_cv.pdf` calls the same command.

## Apply

```bash
rolefit apply --dry-run --limit 5 --yes
rolefit apply --roles senior-python-engineer --limit 10 --min-match-score 80
rolefit apply --submit --limit 3 --yes
```

What a run does:

1. Reads `manifest.json` and asks which role profiles to use when there is more than one.
2. Searches each configured board. Greenhouse uses the public jobs API, one request per board token in `preferences.greenhouse_boards` (for example `stripe`, `airbnb`).
3. Scores each description against that role's tailored CV. Jobs under the match threshold are logged as `below_threshold` and skipped.
4. Uploads that role's PDF, fills name, email, phone, LinkedIn, and location from the CV, and drafts other short answers from the same text.
5. Pauses when the page shows a CAPTCHA or a sign-in wall. Voluntary demographic questions and agreement checkboxes are left for you.
6. Stops before submit unless you passed `--submit`.

`python scripts/apply_jobs.py` calls the same command.

Work-authorization Yes/No answers are filled only when the CV states them. A question about a country the résumé does not mention is left for you. Salary is filled only when the CV states it.

The first time a form asks you to sign in, sign in in the browser window and press Enter. The Playwright profile in `output/browser_profile` is reused. Use a visible browser (`HEADLESS=false`) for sign-in and CAPTCHA.

## Logs

`output/logs/applications.csv` is the spreadsheet copy. `output/logs/applications.db` stores the same rows. On startup the CSV replaces the database, so deleting a row and running again clears that URL.

| Column | Meaning |
| --- | --- |
| Job Title, Company, URL | Opening that was reviewed |
| CV Used | Tailored PDF path |
| Match Score | 0–100 |
| Timestamp | UTC time |
| Status | `submitted`, `pending_review`, `dry_run`, `below_threshold`, `skipped_manual`, or `error` |
| Notes | Short reason |

A URL marked `submitted` or `pending_review` is not applied to again. A later dry run cannot overwrite a submission. `error` can be retried.

## Development

```bash
make lint
make typecheck
make test
```

The tests use fake model responses and a fake browser session. They do not call an API or open Chromium. GitHub Actions runs Ruff, Mypy (strict), and pytest on Python 3.11 and 3.12.

```bash
pre-commit install
```

## Conduct

This project uses the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Report conduct issues to the maintainers through the private contact listed on the GitHub repository.

## License

[MIT](LICENSE).
