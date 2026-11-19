# python-automation-jobs

Standalone extract of the job-application scripts from the `systems` repo. Nothing else in that repo is imported by these scripts.

The working tool is a Playwright assistant. It opens each job URL, drafts form answers with OpenAI from a profile file, fills the fields it can see, uploads a CV when a file input is present, and waits until you type `submit` before clicking a submit button.

## Layout

- `assistant.py` — shared form filler. The three copies in `systems/jobs` (`job_apply_assistant.py`, `safe_job_apply_assistant_dev.py`, `safe_job_apply_assistant_va.py`) were the same class with different profiles and URL lists.
- `apply.py` — command-line entry point.
- `examples/` — placeholder profile and URL list, safe to commit.
- `local/` — your profiles and URL lists. Git ignores this directory.
- `assets/cvs/` — put PDFs here. Pass the file with `--cv`. If you omit `--cv`, the assistant also checks `assets/cvs/ANA_CV.pdf`, `assets/cvs/ANA_CV_VA.pdf`, and `assets/cvs/Ana_Fandino_CV.pdf`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Put your OpenAI key in `.env`.

Copy a résumé PDF into `assets/cvs/`. Profiles already extracted:

- `local/profiles/general.txt` — short profile from `job_apply_assistant.py`
- `local/profiles/dev.txt` — long profile from `safe_job_apply_assistant_dev.py`
- `local/urls/boards.txt` — remote-job board list
- `local/urls/dev.txt` — the Publicis front-end role, without the original session query string

## Run

```bash
python apply.py \
  --profile local/profiles/dev.txt \
  --urls local/urls/dev.txt \
  --cv assets/cvs/Ana_Fandino_CV.pdf
```

Drafts are written to `logs/application_drafts_log.json` and `logs/application_drafts_log.csv`.

## Create the GitHub repo

`local/`, `.env`, CV PDFs, and log files are gitignored so a public push does not include the résumé text or application drafts.

```bash
cd ~/Desktop/python-automation-jobs
git init
git add .
git commit -m "Add job application assistant"
gh repo create python-automation-jobs --private --source=. --push
```

Use `--public` only after you confirm `git status` does not list `local/` or any PDF.
