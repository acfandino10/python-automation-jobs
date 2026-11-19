"""Typer commands for generating tailored CVs and applying with them.

``rolefit generate`` and ``rolefit apply`` are the supported entrypoints.
``scripts/generate_cvs.py`` and ``scripts/apply_jobs.py`` call the same functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from rolefit import __version__
from rolefit.applier import (
    ApplySummary,
    LLMAnswerer,
    LLMJobScorer,
    between_applications,
    build_boards,
    run_apply,
)
from rolefit.config import (
    CandidateProfile,
    Settings,
    load_profile,
    load_settings,
    merge_profile,
    require_applicant,
)
from rolefit.exceptions import ConfigError, RolefitError
from rolefit.llm import LLMClient, build_llm
from rolefit.models import Applicant, Manifest, ManifestRole, RoleSuggestion
from rolefit.parser import applicant_from_text, parse_cv
from rolefit.tailor import generate_tailored_cvs, load_manifest, load_tailored_cv, suggest_roles
from rolefit.tracker import ApplicationTracker

console = Console()
app = typer.Typer(
    name="rolefit",
    help="Tailor a master CV for specific roles and apply with the matching PDF.",
    no_args_is_help=True,
    add_completion=False,
)


def _applicant_from_manifest(document: Manifest) -> Applicant:
    """Re-read contact details from the source CV, then fall back to the manifest."""
    source = Path(document.source_cv) if document.source_cv else None
    if source is not None and source.is_file():
        return applicant_from_text(parse_cv(source).raw_text)
    return document.applicant


def _fail(exc: RolefitError) -> NoReturn:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1)


def _llm_from_settings(settings: Settings) -> LLMClient:
    return build_llm(settings.llm_provider, settings.api_key(), settings.resolved_model())


def _split_roles(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _confirm_titles(titles: list[str], *, assume_yes: bool) -> list[str]:
    if assume_yes:
        return titles
    console.print("Roles to generate:")
    for index, title in enumerate(titles, start=1):
        console.print(f"  {index}. {title}")
    accepted = typer.confirm("Generate a CV for each of these roles?", default=True)
    if not accepted:
        raise ConfigError("No CVs were written.")
    return titles


def _interactive_roles(suggestions: list[RoleSuggestion]) -> list[str]:
    """Let the operator edit the suggested titles and choose how many to keep."""
    import questionary

    console.print("\nSuggested roles from the master CV:\n")
    for index, role in enumerate(suggestions, start=1):
        reason = f" — {role.reason}" if role.reason else ""
        console.print(f"  {index}. {role.title}{reason}")
    use_suggestions = questionary.confirm(
        "Use these suggestions?",
        default=True,
    ).ask()
    if use_suggestions is None:
        raise ConfigError("Role selection was cancelled.")
    if use_suggestions:
        titles = [role.title for role in suggestions]
    else:
        custom = questionary.text(
            "Type role titles, separated by commas",
        ).ask()
        if not custom:
            raise ConfigError("No role titles were entered.")
        titles = _split_roles(str(custom))
    count_answer = questionary.text(
        f"How many roles should be generated? (1-{len(titles)})",
        default=str(min(len(titles), 5)),
    ).ask()
    if count_answer is None:
        raise ConfigError("Role selection was cancelled.")
    try:
        count = int(str(count_answer).strip())
    except ValueError as exc:
        raise ConfigError("The role count must be a whole number.") from exc
    if count < 1 or count > len(titles):
        raise ConfigError(f"Choose a role count between 1 and {len(titles)}.")
    chosen = titles[:count]
    confirmed = questionary.confirm(
        "Generate CVs for: " + ", ".join(chosen) + "?",
        default=True,
    ).ask()
    if not confirmed:
        raise ConfigError("No CVs were written.")
    return chosen


def _select_manifest_roles(
    manifest: Manifest,
    requested: list[str],
    *,
    assume_yes: bool,
) -> list[ManifestRole]:
    if requested:
        chosen: list[ManifestRole] = []
        for query in requested:
            role = manifest.role_by_query(query)
            if role is None:
                known = ", ".join(role.slug for role in manifest.roles) or "(none)"
                raise ConfigError(f"Unknown role '{query}'. Manifest roles: {known}.")
            chosen.append(role)
        return chosen
    if len(manifest.roles) == 1 or assume_yes:
        return list(manifest.roles)
    import questionary

    choices = [
        questionary.Choice(f"{role.title} ({role.slug})", value=role.slug)
        for role in manifest.roles
    ]
    picked = questionary.checkbox("Which role profiles should run?", choices=choices).ask()
    if not picked:
        raise ConfigError("No role profiles were selected.")
    selected = []
    for slug in picked:
        role = manifest.role_by_query(str(slug))
        if role is not None:
            selected.append(role)
    return selected


@app.command("generate")
def generate(
    cv: Annotated[Path, typer.Option("--cv", help="Master CV (.pdf, .docx, or .txt).")],
    roles: Annotated[
        str,
        typer.Option("--roles", help="Comma-separated role titles. Skips the suggestion prompt."),
    ] = "",
    count: Annotated[
        int,
        typer.Option("--count", min=1, max=7, help="How many suggested roles to keep."),
    ] = 6,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    suggest_only: Annotated[
        bool,
        typer.Option("--suggest-only", help="Print role suggestions and do not write PDFs."),
    ] = False,
    output: Annotated[
        Path,
        typer.Option("--output", help="Directory for tailored PDFs and manifest.json."),
    ] = Path("output/tailored_cvs"),
    profile: Annotated[
        Path,
        typer.Option("--profile", help="Candidate profile YAML."),
    ] = Path("profile.yaml"),
    pdf_engine: Annotated[
        str,
        typer.Option("--pdf-engine", help="auto, weasyprint, or reportlab."),
    ] = "",
) -> None:
    """Parse a master CV and write one grounded PDF per confirmed role."""
    try:
        settings = load_settings()
        candidate = merge_profile(settings, load_profile(profile))
        parsed = parse_cv(cv)
        llm = _llm_from_settings(settings)
        if roles.strip():
            titles = _split_roles(roles)
            titles = _confirm_titles(titles, assume_yes=yes)
        else:
            suggestions = suggest_roles(llm, parsed.raw_text, count=count)
            if suggest_only:
                for role in suggestions:
                    console.print(f"- {role.title}: {role.reason}")
                return
            if yes:
                titles = [role.title for role in suggestions[:count]]
            else:
                titles = _interactive_roles(suggestions)
        if suggest_only:
            return
        engine = pdf_engine.strip().lower() or settings.pdf_engine
        manifest = generate_tailored_cvs(
            source_path=cv,
            source_text=parsed.raw_text,
            titles=titles,
            applicant=applicant_from_text(parsed.raw_text),
            llm=llm,
            output_dir=output,
            pdf_engine=engine,
            provider=settings.llm_provider,
            model=settings.resolved_model(),
            location=candidate.preferences.job_location,
        )
    except RolefitError as exc:
        _fail(exc)
        return
    table = Table(title="Tailored CVs")
    table.add_column("Role")
    table.add_column("PDF")
    for generated in manifest.roles:
        table.add_row(generated.title, generated.pdf_path)
    console.print(table)
    console.print(f"Manifest: {output / 'manifest.json'}")


@app.command("apply")
def apply(
    manifest: Annotated[
        Path,
        typer.Option("--manifest", help="Path to manifest.json from rolefit generate."),
    ] = Path("output/tailored_cvs/manifest.json"),
    roles: Annotated[
        str,
        typer.Option("--roles", help="Comma-separated role slugs or titles. Default: ask."),
    ] = "",
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="Maximum application attempts.")
    ] = 10,
    min_match_score: Annotated[
        int | None,
        typer.Option(
            "--min-match-score", min=0, max=100, help="Override the profile match threshold."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fill forms and log them without submitting."),
    ] = False,
    submit: Annotated[
        bool,
        typer.Option(
            "--submit",
            help="Click submit after a form is filled. The default stops for a person to review.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Run every role in the manifest.")] = False,
    profile: Annotated[Path, typer.Option("--profile", help="Candidate profile YAML.")] = Path(
        "profile.yaml"
    ),
    log_dir: Annotated[
        Path,
        typer.Option("--log-dir", help="Directory for applications.db and applications.csv."),
    ] = Path("output/logs"),
) -> None:
    """Score jobs and apply with the PDF that matches each selected role."""
    try:
        settings = load_settings()
        candidate = merge_profile(settings, load_profile(profile))
        document = load_manifest(manifest)
        applicant = require_applicant(_applicant_from_manifest(document))
        selected = _select_manifest_roles(document, _split_roles(roles), assume_yes=yes)
        llm = _llm_from_settings(settings)
        threshold = (
            min_match_score
            if min_match_score is not None
            else candidate.preferences.match_threshold
        )
        summary = _run(
            settings=settings,
            candidate=candidate,
            roles=selected,
            applicant=applicant,
            llm=llm,
            threshold=threshold,
            limit=limit,
            dry_run=dry_run,
            auto_submit=submit,
            log_dir=log_dir,
        )
    except RolefitError as exc:
        _fail(exc)
        return
    _print_summary(summary)


def _run(
    *,
    settings: Settings,
    candidate: CandidateProfile,
    roles: list[ManifestRole],
    applicant: Applicant,
    llm: LLMClient,
    threshold: int,
    limit: int,
    dry_run: bool,
    auto_submit: bool,
    log_dir: Path,
) -> ApplySummary:
    boards = build_boards(candidate, settings)
    with ApplicationTracker(log_dir) as tracker:
        return run_apply(
            roles=roles,
            boards=boards,
            scorer=LLMJobScorer(llm),
            answerer=LLMAnswerer(llm),
            tracker=tracker,
            applicant=applicant,
            threshold=threshold,
            limit=limit,
            dry_run=dry_run,
            auto_submit=auto_submit,
            load_cv=load_tailored_cv,
            remote_only=candidate.preferences.remote_only,
            wait=between_applications(
                settings.min_between_applications,
                settings.max_between_applications,
            ),
        )


def _print_summary(summary: ApplySummary) -> None:
    table = Table(title="Applications")
    table.add_column("Status")
    table.add_column("Score")
    table.add_column("Job")
    table.add_column("URL")
    for record in summary.records:
        score = "" if record.match_score is None else f"{record.match_score:.0f}"
        table.add_row(record.status, score, record.job_title, record.url)
    console.print(table)
    console.print(
        f"Considered {summary.considered}, attempted {summary.attempted}, "
        f"skipped {summary.skipped}."
    )


@app.command("boards")
def boards() -> None:
    """List registered job-board adapters."""
    from rolefit.boards import available_boards

    names = available_boards()
    if not names:
        console.print("No job boards are registered.")
        return
    for name in names:
        console.print(name)


@app.command("version")
def version() -> None:
    """Print the installed Rolefit version."""
    console.print(__version__)
