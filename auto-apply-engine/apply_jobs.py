#!/usr/bin/env python3
"""Search roles from the manifest and fill applications with the matching PDF."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Settings, load_settings  # noqa: E402
from src.cv_tailor import load_manifest  # noqa: E402
from src.errors import AutoApplyError, ConfigError  # noqa: E402
from src.job_applier import JobApplier  # noqa: E402
from src.llm import build_llm  # noqa: E402
from src.models import Manifest, ManifestRole  # noqa: E402
from src.people import PersonRun, applicant_for_run, person_runs, settings_for_person  # noqa: E402
from src.tracking import ApplicationLog, start_of_local_day  # noqa: E402

console = Console()


def main() -> None:
    _require_python()
    args = _parser().parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        settings = load_settings()
        if args.headless:
            settings.headless = True
        if args.auto_submit and args.dry_run:
            console.print("[yellow]--dry-run is on, so nothing will be submitted.[/yellow]")
        if not args.dry_run and not args.auto_submit and not sys.stdin.isatty():
            raise ConfigError(
                "Live runs ask before each submit. Pass --dry-run or --auto-submit for a non-interactive terminal."
            )
        threshold = settings.match_threshold if args.threshold is None else args.threshold
        if not 0 <= threshold <= 100:
            raise ConfigError("--threshold must be between 0 and 100.")
        runs = _runs(args, settings)
        if args.urls and len(runs) > 1:
            raise ConfigError("Pass --manifest for one person when you use --urls.")
        llm = build_llm(settings)
        failures = 0
        for run in runs:
            try:
                _apply_run(args, settings, llm, run, threshold)
            except AutoApplyError as exc:
                console.print(f"[red]{exc}[/red]")
                failures += 1
        if failures:
            raise SystemExit(1)
    except AutoApplyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply to jobs with the CV tailored for each person and role.")
    parser.add_argument(
        "--manifest",
        default="",
        help="One manifest.json. Omit this to apply every person under output/people, or the single output/tailored_cvs manifest.",
    )
    parser.add_argument("--roles", default="", help="Comma-separated role titles or slugs from the manifest.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum matching applications to process this run.")
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=None,
        help="Maximum submitted applications for the local calendar day, across every run.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Score jobs and log them without opening application forms.")
    parser.add_argument(
        "--auto-submit",
        action="store_true",
        help="Click submit on a recognized form. CAPTCHA, login, and unanswered required fields still pause.",
    )
    parser.add_argument("--threshold", type=int, default=None, help="Minimum CV match score. Defaults to MATCH_THRESHOLD.")
    parser.add_argument("--boards", default="", help="Comma-separated override of SEARCH_BOARDS.")
    parser.add_argument("--urls", default="", help="Text file of job URLs. Uses one selected role's PDF for every URL.")
    parser.add_argument("--headless", action="store_true", help="Hide the browser. Sign-in and CAPTCHA still need a visible browser.")
    return parser


def _runs(args, settings: Settings) -> list[PersonRun]:
    if args.manifest.strip():
        return [
            PersonRun(
                slug="",
                manifest_path=settings.resolve(args.manifest),
                log_dir=settings.resolve("output/logs"),
                browser_profile_dir=settings.resolve(settings.browser_profile_dir),
            )
        ]
    found = person_runs(settings.project_root)
    if found:
        legacy = settings.project_root / "output" / "tailored_cvs" / "manifest.json"
        if legacy.exists():
            console.print(
                "[yellow]Applying each person under output/people. "
                "The older output/tailored_cvs manifest is not used.[/yellow]"
            )
        return found
    return [
        PersonRun(
            slug="",
            manifest_path=settings.resolve("output/tailored_cvs/manifest.json"),
            log_dir=settings.resolve("output/logs"),
            browser_profile_dir=settings.resolve(settings.browser_profile_dir),
        )
    ]


def _apply_run(args, settings: Settings, llm, run: PersonRun, threshold: int) -> None:
    manifest = load_manifest(run.manifest_path)
    source = settings.resolve(manifest.source_cv) if manifest.source_cv else Path()
    applicant = applicant_for_run(manifest.applicant, source)
    slug = run.slug or manifest.person_slug
    label = applicant.full_name or slug or manifest.source_cv
    if slug:
        console.print(f"\n[bold]{label}[/bold]")
        browser_dir = run.browser_profile_dir if run.slug else (
            settings.project_root / "output" / "people" / slug / "browser_profile"
        )
        log_dir = run.log_dir if run.slug else settings.project_root / "output" / "people" / slug / "logs"
        run_settings = settings_for_person(settings, applicant, browser_dir)
    else:
        log_dir = run.log_dir
        run_settings = settings
    roles = _select_roles(manifest, args.roles, label if slug else "")
    urls = settings.resolve(args.urls) if args.urls else None
    if urls and len(roles) != 1:
        raise ConfigError("Pass a single --roles value when you use --urls, so the matching PDF is unambiguous.")
    boards = _boards(args.boards, run_settings)
    log = ApplicationLog(log_dir)
    try:
        run_limit = _run_limit(args.limit, args.daily_limit, log)
        if run_limit == 0:
            console.print(
                f"Daily limit of {args.daily_limit} submitted application(s) is already reached. Nothing to do."
            )
            return
        applier = JobApplier(run_settings, llm, log, console)
        console.print(
            f"Match threshold [bold]{threshold}[/bold]. "
            f"This run [bold]{run_limit}[/bold]."
            + (f" Daily cap [bold]{args.daily_limit}[/bold]." if args.daily_limit else "")
            + " "
            + (
                "Dry run."
                if args.dry_run
                else "Submit stays manual until you type submit."
                if not args.auto_submit
                else "Auto-submit is on for recognized forms."
            )
        )
        outcomes = applier.run(
            roles,
            limit=run_limit,
            dry_run=args.dry_run,
            auto_submit=args.auto_submit,
            boards=boards,
            threshold=threshold,
            urls_file=urls,
            applicant=applicant,
        )
        _print_summary(outcomes, log.csv_path)
    finally:
        log.close()


def _select_roles(manifest: Manifest, query: str, label: str = "") -> list[ManifestRole]:
    if not manifest.roles:
        raise ConfigError("The manifest has no roles. Run generate_cvs.py first.")
    if query.strip():
        return _roles_from_query(manifest, query)
    if len(manifest.roles) == 1:
        return list(manifest.roles)
    if not sys.stdin.isatty():
        known = ", ".join(role.slug for role in manifest.roles)
        raise ConfigError(f"Pass --roles. Available slugs: {known}")
    import questionary

    choices = [questionary.Choice(role.title, value=role.slug) for role in manifest.roles]
    prompt = f"Which roles should I run for {label}?" if label else "Which roles should I run?"
    selected = questionary.checkbox(prompt, choices=choices).ask()
    if selected is None:
        raise SystemExit(130)
    if not selected:
        raise ConfigError("Select at least one role.")
    return [role for role in manifest.roles if role.slug in set(selected)]


def _roles_from_query(manifest: Manifest, query: str) -> list[ManifestRole]:
    chosen: list[ManifestRole] = []
    for part in query.split(","):
        role = manifest.role_by_query(part)
        if role is None:
            known = ", ".join(f"{item.title} ({item.slug})" for item in manifest.roles)
            raise ConfigError(f"Unknown role '{part.strip()}'. Manifest has: {known}")
        if all(role.slug != existing.slug for existing in chosen):
            chosen.append(role)
    return chosen


def _boards(raw: str, settings) -> list[str]:
    if not raw.strip():
        return settings.board_list()
    settings.search_boards = raw
    return settings.board_list()


def _run_limit(per_run: int, daily_limit: int | None, log: ApplicationLog) -> int:
    if per_run < 1:
        raise ConfigError("--limit must be at least 1.")
    if daily_limit is None:
        return per_run
    if daily_limit < 1:
        raise ConfigError("--daily-limit must be at least 1.")
    used = log.submitted_since(start_of_local_day())
    remaining = daily_limit - used
    console.print(f"Submitted today: [bold]{used}[/bold] of [bold]{daily_limit}[/bold].")
    if remaining < 1:
        return 0
    return min(per_run, remaining)


def _print_summary(outcomes, csv_path: Path) -> None:
    if not outcomes:
        console.print("No new applications were logged.")
        console.print(f"Log: [bold]{csv_path}[/bold]")
        return
    table = Table(title="This run")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Job")
    table.add_column("Company")
    for record in outcomes:
        score = "" if record.match_score is None else f"{record.match_score:.0f}"
        table.add_row(record.status, score, record.job_title, record.company)
    console.print(table)
    console.print(f"Log: [bold]{csv_path}[/bold]")


def _require_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")


if __name__ == "__main__":
    main()
