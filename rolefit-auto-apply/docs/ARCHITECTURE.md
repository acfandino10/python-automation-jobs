# Architecture

Rolefit is two pipelines that share a manifest. `rolefit generate` writes the manifest. `rolefit apply` reads it and refuses to upload any PDF other than the one stored for the role being searched.

## Data flow

```text
profile.yaml + .env
        |
        v
CandidateProfile  -------------------> form answers and search preferences

input/base_cv.pdf
        |
        v
ExtractorRegistry.extract  ->  raw text
        |
        +--> structure_text -> CVData (heuristic JSON)
        |
        v
suggest_roles (LLM) -> operator confirms titles
        |
        v
tailor_cv (LLM) -> build_tailored_cv
        |
        v
grounding drops unknown employers, skills, and numbers
        |
        v
templates/ats_clean.html  ->  HTML
        |
        +--> WeasyPrint, or ReportLab when WeasyPrint is unavailable
        |
        v
output/tailored_cvs/{slug}_cv.pdf
output/tailored_cvs/{slug}_cv.json
output/tailored_cvs/manifest.json
        |
        v
BaseJobBoard.search(SearchQuery)
        |
        v
score 0-100 against the tailored CV
        |
        +--> below threshold: log and continue
        |
        v
BaseJobBoard.apply(job, ApplyContext)
        |
        v
output/logs/applications.db
output/logs/applications.csv
```

`CVData.raw_text` is the grounding source. The heuristic sections are a convenience for inspection. They are not trusted over the raw extraction.

## Packages

| Module | Responsibility |
| --- | --- |
| `rolefit.cli` | Typer commands. Interactive prompts stay here. |
| `rolefit.config` | pydantic-settings and the YAML profile. |
| `rolefit.models` | `CVData`, `TailoredCV`, `JobListing`, `Manifest`, `ApplicationRecord`. |
| `rolefit.parser` | PDF, DOCX, and text extractors. New formats register a `DocumentExtractor`. |
| `rolefit.llm` | `LLMClient` protocol and the OpenAI, Anthropic, and Gemini implementation. |
| `rolefit.tailor` | Prompts, grounding, Jinja rendering, PDF writing, manifest updates. |
| `rolefit.forms` | Label mapping, work-authorization Yes/No, CAPTCHA and login detection. |
| `rolefit.applier` | Score, skip duplicates, call a board, write the log. |
| `rolefit.tracker` | SQLite plus CSV. The CSV is reloaded on startup. |
| `rolefit.boards` | Plugin package. `base.py` is the contract. `greenhouse.py` is the example. |

`scripts/generate_cvs.py` and `scripts/apply_jobs.py` insert `src` on `sys.path` and invoke the Typer app, so a checkout runs before an editable install.

## Manifest

`manifest.json` is the contract between the two commands. Each `ManifestRole` stores the title, slug, PDF path, HTML path, JSON path, keywords, and location. Apply loads `{slug}_cv.json` back into a `TailoredCV` for scoring and screening answers, and uploads `pdf_path` as the file.

Slugs are stable and unique inside one generate run: `Data Engineer` becomes `data-engineer`, and a second identical title becomes `data-engineer-2`.

## Grounding

`build_tailored_cv` keeps the operator's name and contact line from the profile. It then:

- Drops a job whose company is not mentioned in the source text.
- Drops a bullet, date, or detail that contains a number the source does not contain.
- Drops a skill that is not mentioned in the source.
- Drops a school or project that is not mentioned in the source.
- Builds `search_keywords` from the target role plus skills and phrases that are actually in the source.

If the model returns employers and every one of them is unknown, tailoring raises `CVError` and does not write a PDF.

## Job-board plugin

`BaseJobBoard` is the strategy interface.

```python
class BaseJobBoard(ABC):
    name: str

    def search(self, query: SearchQuery) -> list[JobListing]: ...

    def apply(self, job: JobListing, context: ApplyContext) -> ApplyResult: ...

    def pause_for_human(self, reason: str) -> None: ...
```

`SearchQuery` carries the role title, location, keywords from the tailored CV, `remote_only`, and a limit.

`ApplyContext` carries the applicant, the tailored CV, the PDF path, `dry_run`, `auto_submit`, and `answer(question) -> ScreeningAnswer`.

Register with `@register_board`. Importing `rolefit.boards` registers Greenhouse. `rolefit boards` prints the registry. `build_boards` rejects a name that is not registered.

### What an adapter should do

- List with a public API when one exists. Make one request per company token, not a loop of result pages, unless the API paginates and the limit requires another page.
- Return `JobListing` values with absolute `http` or `https` URLs.
- Open the application in a persistent browser profile so a person signs in once.
- Detect CAPTCHA and login walls with `needs_human` and pause. Do not attempt to solve them.
- Upload `context.pdf_path` and no other file.
- Fill contact fields from the profile. Use `context.answer` for everything else. That helper refuses demographic questions, agreements, and identifiers such as a social security number.
- Return `dry_run` when asked not to submit. Return `pending_review` when a person must finish the form. Return `submitted` only after a real submit control has been clicked and the operator passed `--submit`.

### Greenhouse

`GreenhouseBoard.search` calls `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`. HTML descriptions are reduced to text. `keyword_match` drops postings that share neither a keyword nor a distinctive word from the role title. `remote_only` keeps locations that say remote, or that contain the requested location string.

`GreenhouseBoard.apply` uses `PageSession`. `PlaywrightPageSession` is the production implementation: `launch_persistent_context` on `output/browser_profile`. Tests pass a fake session and an `httpx.MockTransport`, so CI does not install Chromium or call Greenhouse.

## Application log

`ApplicationTracker` uses `applications.db` with `url` as the primary key. Every write rewrites `applications.csv`. Opening the tracker loads the CSV back into SQLite when the file exists, which is how deleting a spreadsheet row unlocks a URL.

`submitted` is sticky: a later `dry_run` or `error` does not replace it. `is_blocked` is true for `submitted` and `pending_review`. `run_apply` also ignores a URL it has already handled in the same process, after `canonical_url` strips campaign parameters. Indeed's `jk` parameter is kept because it identifies the job.

`--limit` counts attempts that reach `apply`, including dry runs. Below-threshold jobs and duplicates do not consume the limit.

## Configuration

`.env` holds secrets and runtime knobs: provider, API key, model, browser directory, delays, PDF engine, and optional search overrides. `profile.yaml` holds search preferences. Name, email, phone, links, location, work authorization, and salary are parsed from the CV and are not read from the environment. `remote_only` and `match_threshold` override the profile only when the environment set them, because their settings values are `None` until then.

Between attempts, the CLI sleeps a random interval between `MIN_BETWEEN_APPLICATIONS` and `MAX_BETWEEN_APPLICATIONS` (defaults 6 and 14 seconds). Tests replace that wait with a no-op.

## PDF rendering

`templates/ats_clean.html` is a single-column document: one `h1`, section `h2` elements, and lists. No tables, no columns, no images. WeasyPrint prints that HTML when `PDF_ENGINE` is `weasyprint`, or when it is `auto` and WeasyPrint imports. Otherwise ReportLab draws the same sections from the `TailoredCV` object, so the text layer does not depend on system Cairo libraries. The wheel build copies the template into the package. A source checkout reads `templates/ats_clean.html` directly.

## Testing

`tests/test_cv_parser.py` builds a PDF and a DOCX in a temporary directory and registers a custom extractor.

`tests/test_cv_tailor.py` feeds a model payload that invents an employer, a metric, a school, and a skill. The assertions check that those claims are absent from the model object, the HTML, and the extracted PDF text.

`tests/test_job_applier.py` covers the log, profile Yes/No mapping, Greenhouse filtering through `httpx.MockTransport`, CAPTCHA pause, demographic pause, threshold skips, and the rule that a submitted URL is not opened again.

None of these tests read `OPENAI_API_KEY` or start Playwright.
