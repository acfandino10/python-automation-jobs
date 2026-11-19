# rolefit-auto-apply

CLI that tailors each résumé in `input/` for several target roles, then fills job applications with the PDF that matches each role. More than one file in `input/` is more than one person.

Suggested repository name: `rolefit-auto-apply`.

The tailor rewrites the summary and reorders bullets for the role. It keeps only employers, schools, projects, skills, and numbers that already appear in the source CV. A live run fills forms from that CV, then waits for you to type `submit`. `--auto-submit` sends a recognized form. Sign-in walls, visible CAPTCHAs, and required fields the tool cannot answer always pause for you.

You are responsible for each job board's terms and for every answer that gets sent. Start with `--dry-run`.

## Layout

```text
auto-apply-engine/
├── input/                     # One résumé per person (PDF, DOCX, or TXT)
├── output/
│   ├── tailored_cvs/          # Used when input/ has a single résumé
│   ├── people/<person>/       # Used when input/ has more than one résumé
│   │   ├── tailored_cvs/
│   │   ├── logs/
│   │   └── browser_profile/
│   ├── logs/                  # Application log for a single résumé
│   └── browser_profile/       # Browser session for a single résumé
├── src/
│   ├── config.py              # API key, search, and browser settings
│   ├── cv_parser.py           # PDF / DOCX text extraction
│   ├── cv_tailor.py           # Role rewrite and ATS PDF rendering
│   ├── job_applier.py         # Search, match, and form filling
│   ├── llm.py                 # OpenAI, Anthropic, and Gemini
│   └── tracking.py            # CSV + SQLite application log
├── generate_cvs.py
├── apply_jobs.py
├── .env.example
└── requirements.txt
```

## Setup

Use Python 3.11 or newer.

```bash
cd auto-apply-engine
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Put your API key in `.env`. Copy each person's résumé into `input/`. A PDF, DOCX, or TXT file works. One file is one person. Two files are two people, and the filename is what separates them (`Ana_Fandino_CV.pdf`, `Camila_Fandino_CV.pdf`). Name, email, phone, LinkedIn, location, work authorization, and salary are read from that résumé. If a field is missing, put it in a file with the same name and a `.profile` extension, for example `input/Camila_Fandino_CV.profile` with `EMAIL=` and `PHONE=`. Scanned image PDFs are rejected because they have no text layer.

`PDF_ENGINE=auto` writes the PDF with ReportLab. Install WeasyPrint and its system libraries, then set `PDF_ENGINE=weasyprint`, if you want the HTML template rendered directly.

## Generate tailored CVs

```bash
python generate_cvs.py
```

Every résumé in `input/` is tailored. One file writes to `output/tailored_cvs/`. More than one file writes each person to `output/people/<person>/tailored_cvs/`. The same `--roles` list is used for every person. Without `--roles`, the prompt asks for titles once per person.

The prompt can suggest 6 roles from the CV or take titles you type. It asks how many CVs to produce and asks you to confirm the list. Each role is saved as:

- `output/tailored_cvs/{role-slug}_cv.pdf` for one person
- `output/people/<person>/tailored_cvs/{role-slug}_cv.pdf` when `input/` has several people
- the matching `.html`, `.json`, and `manifest.json` in that same directory

Non-interactive example:

```bash
python generate_cvs.py \
  --cv input/base_cv.pdf \
  --roles "Senior Python Engineer,Data Engineer" \
  --yes
```

`--suggest-only` prints role ideas and does not write PDFs.

## Apply

```bash
python apply_jobs.py --dry-run --limit 5
python apply_jobs.py --roles senior-python-engineer --limit 10
python apply_jobs.py --auto-submit --limit 3
```

What a run does:

1. Loads one manifest, or every manifest under `output/people/` when more than one person was generated. Each person is applied with their own name and email, their own log, and their own browser login.
2. Searches Indeed and LinkedIn in a persistent Chromium profile (`SEARCH_BOARDS`). The first time a board asks you to sign in, sign in in that window and press Enter. The session is reused.
3. Greenhouse is included when `SEARCH_BOARDS` contains `greenhouse` and `GREENHOUSE_BOARDS` lists public board slugs such as `stripe,airbnb`. Listing uses Greenhouse's public jobs API. The application form still opens in the browser.
4. Scores each description against the tailored CV. Jobs under `MATCH_THRESHOLD` (default 75) are logged as `below_threshold` and skipped.
5. Uploads that role's PDF, fills name, email, phone, LinkedIn, and location from that person's résumé, and drafts other short answers from the same text.
6. Waits for you to type `submit` unless you passed `--auto-submit`.

`--urls jobs.txt` skips search and opens the listed URLs with one role's PDF. Pass a single `--roles` value with it.

`--headless` hides the browser. Use a visible browser when a site asks you to sign in or complete a check.

`--daily-limit` caps submitted applications for the machine's calendar day. A later run the same day stops when that number is already in the log. With several people, each person has their own log, so the cap is counted separately. `--limit` still caps one person's part of the run.

Work-authorization Yes/No answers are filled only when the CV states them. A question about a country the résumé does not mention is left for you. Salary questions are filled only when the CV states a salary. Voluntary demographic questions and agreement checkboxes are left for you.

## Run on a schedule

Schedule `apply_jobs.py`. Generate the tailored PDFs once with `generate_cvs.py`, and again when the base CV or the target roles change. Cron has no terminal, so the scheduled command uses `--auto-submit`, `--headless`, and an explicit `--roles` value.

Two numbers are independent:

- The cron time is when a run starts.
- `DAILY_LIMIT` is how many applications may be submitted that calendar day.
- `RUN_LIMIT` is how many of those a single wake-up may submit. The day stops at `DAILY_LIMIT` even if cron fires again.

Sign in once before turning the schedule on, in a visible browser:

```bash
python apply_jobs.py --roles senior-python-engineer --limit 1
```

Use a slug from that person's `manifest.json`. After that login, the session in `output/browser_profile/` (one person) or `output/people/<person>/browser_profile/` (several people) is what cron reuses. A scheduled run applies the same roles for each person, and each person has their own daily count.

Edit `scripts/apply_cron.sh` or export the variables. This example allows 10 submitted applications per day, with at most 4 each time the job wakes up:

```bash
ROLES=senior-python-engineer
DAILY_LIMIT=10
RUN_LIMIT=4
```

Open the crontab:

```bash
crontab -e
```

A line has five schedule fields, then the command. The fields are minute, hour, day of month, month, and day of week. `1-5` is Monday through Friday. These three examples all keep the daily cap at 10:

```cron
# Once a weekday at 09:00. One run can use the whole daily cap.
0 9 * * 1-5 ROLES=senior-python-engineer DAILY_LIMIT=10 RUN_LIMIT=10 /bin/bash /Users/anaca/Desktop/python-automation-jobs/auto-apply-engine/scripts/apply_cron.sh >> /Users/anaca/Desktop/python-automation-jobs/auto-apply-engine/output/logs/cron.log 2>&1

# Every 2 hours from 09:00 through 17:00. Each wake-up submits at most 4, and the day still stops at 10.
0 9-17/2 * * 1-5 ROLES=senior-python-engineer DAILY_LIMIT=10 RUN_LIMIT=4 /bin/bash /Users/anaca/Desktop/python-automation-jobs/auto-apply-engine/scripts/apply_cron.sh >> /Users/anaca/Desktop/python-automation-jobs/auto-apply-engine/output/logs/cron.log 2>&1

# Every day at 08:30, including weekends.
30 8 * * * ROLES=senior-python-engineer DAILY_LIMIT=10 RUN_LIMIT=10 /bin/bash /Users/anaca/Desktop/python-automation-jobs/auto-apply-engine/scripts/apply_cron.sh >> /Users/anaca/Desktop/python-automation-jobs/auto-apply-engine/output/logs/cron.log 2>&1
```

Replace the role slug with yours. `crontab -l` prints the installed lines. Output from each run is appended to `output/logs/cron.log`.

On this Mac, cron cannot read `Desktop` until `/usr/sbin/cron` is allowed under System Settings → Privacy & Security → Full Disk Access.

A run that finds a sign-in wall or a CAPTCHA exits, because cron cannot click through it. Log in again with the visible command above, then leave the schedule in place. The script also skips a wake-up when a previous run is still holding `output/logs/apply.lock`.

Check the math with a dry run before installing cron:

```bash
python apply_jobs.py --roles senior-python-engineer --dry-run --limit 4 --daily-limit 10
```

## Logs

`output/logs/applications.csv` is the spreadsheet copy when `input/` has one résumé. With several people, each log is `output/people/<person>/logs/applications.csv`. The matching `applications.sqlite3` stores the same rows.

| Column | Meaning |
| --- | --- |
| Job Title, Company, URL | Opening that was reviewed |
| CV Used | Tailored PDF path |
| Match Score | 0–100 from the model |
| Timestamp | UTC time |
| Status | `submitted`, `pending_review`, `dry_run`, `below_threshold`, `skipped_manual`, or `error` |
| Notes | Short reason |

URLs already marked `submitted` or `pending_review` are skipped on the next run. To try one again, delete that row in the CSV and run again. The CSV is reloaded at startup.

## Tests

```bash
pytest
```

The tests cover parsing, grounding, PDF text, form-label mapping, and the application log. They do not call a model or open a browser.

## Limits

Job-board markup changes. When a selector misses, the run logs an error or pauses instead of guessing. The tool does not solve CAPTCHAs, rotate proxies, or bypass sign-in. It will not invent employers, degrees, or metrics that are absent from the source CV.
