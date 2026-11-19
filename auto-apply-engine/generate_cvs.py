#!/usr/bin/env python3
"""Interactive CLI: suggest roles, tailor the base CV, and write one PDF per role."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_settings  # noqa: E402
from src.cv_parser import extract_text  # noqa: E402
from src.cv_tailor import (  # noqa: E402
    assign_slugs,
    load_manifest,
    merge_manifest,
    suggest_roles,
    tailor_cv,
    write_role_outputs,
)
from src.errors import AutoApplyError, CVError  # noqa: E402
from src.llm import build_llm  # noqa: E402
from src.models import ManifestRole  # noqa: E402
from src.people import (  # noqa: E402
    PersonCV,
    discover_cvs,
    person_slug,
    profile_path_for,
    resolve_applicant,
    tailored_output_dir,
    unsupported_names,
)

console = Console()


def main() -> None:
    _require_python()
    args = _parser().parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        settings = load_settings()
        people = _load_people(args, settings)
        if not people:
            raise SystemExit("No CV could be read.")
        _warn_unsupported(settings.project_root / "input")
        _warn_missing_email(people)
        llm = build_llm(settings)
        if args.suggest_only:
            _suggest_all(llm, people)
            return
        plans = _plan(args, llm, people)
        if not plans:
            raise SystemExit("No roles selected.")
        failures = 0
        for person, titles in plans:
            if len(people) > 1:
                label = person.applicant.full_name or person.path.name
                console.print(f"\n[bold]{label}[/bold]  {person.path.name}")
            multiple = len(people) > 1 or not person.solo
            failures += _generate(args, settings, llm, person, titles, multiple=multiple)
        if failures:
            raise SystemExit(1)
    except AutoApplyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tailor each résumé in input/ for one or more roles and write ATS-friendly PDFs."
    )
    parser.add_argument(
        "--cv",
        default="",
        help="One résumé file. Omit this to process every PDF, DOCX, and TXT in input/.",
    )
    parser.add_argument("--roles", default="", help="Comma-separated role titles. Skips the suggestion prompt.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument(
        "--suggest-only",
        action="store_true",
        help="Print suggested roles and exit without writing PDFs.",
    )
    parser.add_argument(
        "--output-dir",
        default="output/tailored_cvs",
        help="Directory for PDFs, HTML, metadata, and manifest.json.",
    )
    return parser


def _choose_titles(args: argparse.Namespace, llm, source_text: str) -> list[str]:
    provided = [part.strip() for part in args.roles.split(",") if part.strip()]
    if args.suggest_only:
        _print_suggestions(suggest_roles(llm, source_text))
        raise SystemExit(0)
    if provided:
        titles = provided
    else:
        if not sys.stdin.isatty():
            raise SystemExit("Pass --roles and --yes when stdin is not a terminal.")
        titles = _prompt_titles(llm, source_text)
    if not titles:
        return []
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Pass --yes to generate without a confirmation prompt.")
        _print_title_table(titles)
        if not _confirm("Generate a CV for each of these roles?"):
            raise SystemExit("Cancelled.")
    return titles[:10]


def _prompt_titles(llm, source_text: str) -> list[str]:
    import questionary

    mode = _ask(
        questionary.select(
            "How should I choose roles?",
            choices=[
                questionary.Choice("Suggest roles from my CV", value="suggest"),
                questionary.Choice("I will type the roles", value="custom"),
            ],
        )
    )
    suggestions = []
    if mode == "suggest":
        try:
            with console.status("Reading the CV and suggesting roles..."):
                suggestions = suggest_roles(llm, source_text)
        except AutoApplyError as exc:
            console.print(f"[red]{exc}[/red]")
            if not _confirm("Enter roles manually instead?", default=True):
                raise SystemExit(1)
        else:
            _print_suggestions(suggestions)

    maximum = 10
    default_count = min(3, len(suggestions) or 3)
    count = _ask_int(
        "How many CVs should I generate?",
        default=default_count,
        minimum=1,
        maximum=maximum,
    )
    titles: list[str] = []
    for index in range(count):
        default = suggestions[index].title if index < len(suggestions) else ""
        title = _ask(questionary.text(f"Role {index + 1}", default=default)).strip()
        if title:
            titles.append(title)
    deduped: list[str] = []
    seen: set[str] = set()
    for title in titles:
        key = title.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(title)
    return deduped


def _load_people(args, settings) -> list[PersonCV]:
    if args.cv.strip():
        path = _resolve_cv(args.cv, settings.project_root)
        text = extract_text(path)
        return [
            PersonCV(
                slug="cv",
                path=path,
                text=text,
                applicant=resolve_applicant(text, profile_path_for(path)),
                solo=True,
            )
        ]
    folder = settings.project_root / "input"
    people = _load_people_skipping_errors(folder)
    if people:
        return people
    raise SystemExit(
        "No CV found in input/. Copy a PDF, DOCX, or TXT there, or pass --cv /path/to/resume.pdf."
    )


def _load_people_skipping_errors(folder: Path) -> list[PersonCV]:
    """Read the résumés that parse. One bad file does not drop the others."""
    paths = discover_cvs(folder)
    solo = len(paths) == 1
    used: set[str] = set()
    people: list[PersonCV] = []
    for path in paths:
        try:
            text = extract_text(path)
        except CVError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        people.append(
            PersonCV(
                slug=person_slug(path, used),
                path=path,
                text=text,
                applicant=resolve_applicant(text, profile_path_for(path)),
                solo=solo,
            )
        )
    return people


def _warn_unsupported(folder: Path) -> None:
    names = unsupported_names(folder)
    if names:
        console.print(
            "[yellow]Skipped unsupported files: "
            + ", ".join(names)
            + ". Use PDF, DOCX, or TXT.[/yellow]"
        )


def _warn_missing_email(people: list[PersonCV]) -> None:
    if len(people) < 2:
        return
    for person in people:
        if person.applicant.email:
            continue
        profile = profile_path_for(person.path)
        console.print(
            f"[yellow]{person.path.name} has no email. "
            f"Add EMAIL= to input/{profile.name} and run generate_cvs.py again "
            "before applying as this person.[/yellow]"
        )


def _suggest_all(llm, people: list[PersonCV]) -> None:
    for person in people:
        console.print(f"\n[bold]{person.path.name}[/bold]")
        _print_suggestions(suggest_roles(llm, person.text))


def _plan(args, llm, people: list[PersonCV]) -> list[tuple[PersonCV, list[str]]]:
    if args.roles.strip() or len(people) == 1:
        titles = _choose_titles(args, llm, people[0].text)
        if not titles:
            return []
        return [(person, titles) for person in people]
    plans = []
    for person in people:
        console.print(f"\n[bold]{person.applicant.full_name or person.path.name}[/bold]")
        titles = _choose_titles(args, llm, person.text)
        if titles:
            plans.append((person, titles))
    return plans


def _generate(args, settings, llm, person: PersonCV, titles: list[str], *, multiple: bool) -> int:
    output_dir = settings.resolve(tailored_output_dir(person.slug, multiple=multiple, output_dir=args.output_dir))
    manifest_path = output_dir / "manifest.json"
    applicant = person.applicant
    source_path = person.path
    source_text = person.text
    failures: list[tuple[str, str]] = []
    written: list[ManifestRole] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Tailoring", total=len(titles))
        for title, slug in assign_slugs(titles):
            progress.update(task, description=f"Tailoring {title}")
            try:
                cv = tailor_cv(llm, source_text, applicant, title)
                pdf_path, html_path, metadata_path, engine = write_role_outputs(
                    cv,
                    output_dir,
                    slug,
                    settings.pdf_engine,
                )
                role = ManifestRole(
                    title=title,
                    slug=slug,
                    pdf_path=_relative(pdf_path, settings.project_root),
                    html_path=_relative(html_path, settings.project_root),
                    metadata_path=_relative(metadata_path, settings.project_root),
                    keywords=cv.search_keywords,
                    location=settings.job_location,
                )
                merge_manifest(
                    manifest_path,
                    source_cv=_relative(source_path, settings.project_root),
                    provider=settings.llm_provider,
                    model=settings.resolved_model(),
                    role=role,
                    person_slug="" if person.solo else person.slug,
                    applicant=applicant,
                )
                written.append(role)
                progress.console.print(f"[green]Saved[/green] {role.pdf_path} ({engine})")
            except AutoApplyError as exc:
                failures.append((title, str(exc)))
                progress.console.print(f"[red]{title}: {exc}[/red]")
            progress.advance(task)

    if written:
        console.print(f"Manifest: [bold]{_relative(manifest_path, settings.project_root)}[/bold]")
        try:
            manifest = load_manifest(manifest_path)
            table = Table(title="Tailored CVs")
            table.add_column("Role")
            table.add_column("PDF")
            table.add_column("Keywords")
            for role in manifest.roles:
                table.add_row(role.title, role.pdf_path, ", ".join(role.keywords))
            console.print(table)
        except AutoApplyError as exc:
            console.print(f"[yellow]Manifest was written but could not be re-read: {exc}[/yellow]")
    return len(failures)


def _print_suggestions(suggestions) -> None:
    table = Table(title="Suggested roles")
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("Why it fits")
    for index, role in enumerate(suggestions, start=1):
        table.add_row(str(index), role.title, role.reason)
    console.print(table)


def _print_title_table(titles: list[str]) -> None:
    table = Table(title="CVs to generate")
    table.add_column("#", justify="right")
    table.add_column("Role")
    for index, title in enumerate(titles, start=1):
        table.add_row(str(index), title)
    console.print(table)


def _resolve_cv(raw: str, root: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    if path.exists():
        return path
    raise SystemExit(
        f"No CV found at {path}. Copy a PDF, DOCX, or TXT into input/, or pass --cv /path/to/resume.pdf."
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _ask(prompt):
    answer = prompt.ask()
    if answer is None:
        raise SystemExit(130)
    return answer


def _confirm(message: str, default: bool = True) -> bool:
    import questionary

    answer = questionary.confirm(message, default=default).ask()
    if answer is None:
        raise SystemExit(130)
    return bool(answer)


def _ask_int(message: str, default: int, minimum: int, maximum: int) -> int:
    import questionary

    while True:
        raw = _ask(questionary.text(message, default=str(default))).strip()
        try:
            value = int(raw)
        except ValueError:
            console.print("Enter a whole number.")
            continue
        if minimum <= value <= maximum:
            return value
        console.print(f"Enter a number from {minimum} to {maximum}.")


def _require_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")


if __name__ == "__main__":
    main()
