# Contributing

Thanks for helping Rolefit stay careful about other people's résumés and about the sites it automates. The most useful contributions are job-board adapters, bug fixes with a failing test, and clearer pauses when a person needs to take over.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
cp profile.example.yaml profile.yaml
pre-commit install
```

Install the optional extras only when you need them:

```bash
python -m pip install -e ".[llm,browser,pdf]"
playwright install chromium
```

## Checks

```bash
make test
make lint
make typecheck
```

`make ci` runs all three. Tests must pass with no network, no API key, and no browser. Mock the language model with a small object that implements `complete_json`, and mock the browser with an object that matches `PageSession` in `src/rolefit/boards/greenhouse.py`.

Ruff formats and lints. Mypy runs in strict mode. New public functions need a Google-style docstring.

## Pull requests

- Keep a PR focused on one board, one bug, or one user-facing change.
- Add or update a test in `tests/` that fails before the fix and passes after it.
- Do not commit `.env`, `profile.yaml`, résumés, `output/`, or browser profiles.
- Fill in the pull request template, including the test plan.

## Add a job-board adapter

Adapters live in `src/rolefit/boards/`. The Greenhouse module is the reference: it lists jobs without a browser, and it fills the form through a `PageSession` so tests never launch Chromium.

1. Create `src/rolefit/boards/lever.py` (the name is an example).
2. Subclass `BaseJobBoard`, set `name = "lever"`, and decorate the class with `@register_board`.
3. Implement `search(self, query: SearchQuery) -> list[JobListing]`. Prefer a public listing API. Cap results at `query.limit`. Filter with `keyword_match` so unrelated postings are not scored.
4. Implement `apply(self, job: JobListing, context: ApplyContext) -> ApplyResult`. Upload `context.pdf_path` unchanged. That file is the PDF generated for the role being searched. Call `context.answer(label)` for screening questions. Call `self.pause_for_human(reason)` for a CAPTCHA, a login wall, or a question `needs_review` marks as unanswered.
5. When `context.dry_run` is true, or `context.auto_submit` is false, do not click submit.
6. Import the module from `src/rolefit/boards/__init__.py` so `rolefit boards` lists it.
7. Add an offline test that feeds a recorded JSON listing and a fake page session. Assert the uploaded path is the role PDF and that a CAPTCHA string pauses instead of submitting.

Sketch:

```python
from rolefit.boards.base import (
    ApplyContext,
    ApplyResult,
    BaseJobBoard,
    SearchQuery,
    register_board,
)
from rolefit.models import JobListing


@register_board
class LeverBoard(BaseJobBoard):
    """Search and apply on Lever postings."""

    name = "lever"

    def search(self, query: SearchQuery) -> list[JobListing]:
        return []

    def apply(self, job: JobListing, context: ApplyContext) -> ApplyResult:
        if context.dry_run or not context.auto_submit:
            return ApplyResult(status="dry_run", notes="Form was not submitted.")
        return ApplyResult(status="pending_review", notes="Review the form before sending it.")
```

Replace the empty methods with a real listing call and a real form fill before opening the PR. Document any token or company slug the user must add under `preferences` in `profile.example.yaml`. `build_boards` constructs Greenhouse itself because that adapter needs board tokens. If your constructor needs more than the pause callback, extend `build_boards` in `src/rolefit/applier.py` and cover that branch with a test.

Users enable the board by adding its `name` to `preferences.search_boards`.

## Tailoring rules

Do not weaken the grounding in `src/rolefit/tailor.py`. A bullet with a number that is absent from the source CV is dropped. An employer, school, or project that the source does not mention is dropped. If that removes every employer, generation fails instead of writing a fabricated PDF.

## Reporting issues

Use the bug and feature templates. Leave out API keys, session cookies, and the text of a real résumé.
