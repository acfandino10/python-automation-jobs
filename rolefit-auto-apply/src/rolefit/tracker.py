"""SQLite application log with a CSV export that can be edited by hand.

A URL recorded as ``submitted`` or ``pending_review`` is never applied to again.
Deleting that row from ``applications.csv`` and reopening the log clears the block.
``error`` rows can be retried. ``dry_run`` and ``below_threshold`` can be followed
by a real submission.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from rolefit.models import ApplicationRecord, ApplicationStatus

CSV_COLUMNS = [
    "Job Title",
    "Company",
    "URL",
    "CV Used",
    "Match Score",
    "Timestamp",
    "Status",
    "Notes",
]

BLOCKING_STATUSES = frozenset({"submitted", "pending_review"})
STATUSES = frozenset(
    {
        "submitted",
        "pending_review",
        "dry_run",
        "below_threshold",
        "skipped_manual",
        "error",
    }
)


def utc_now() -> str:
    """Return the current UTC time as a second-precision ISO timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class ApplicationTracker:
    """One row per job URL.

    A submitted application is never overwritten by a later skip. On startup the
    CSV, when present, replaces the SQLite rows so a hand-edited spreadsheet is
    the source of truth.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = directory / "applications.db"
        self.csv_path = directory / "applications.csv"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._setup()
        if self.csv_path.exists():
            self._replace_from_csv()

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()

    def __enter__(self) -> ApplicationTracker:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def is_blocked(self, url: str) -> bool:
        """Return whether this URL already has a submission or a human review in progress."""
        row = self._conn.execute(
            "SELECT status FROM applications WHERE url = ?",
            (url,),
        ).fetchone()
        return bool(row and row["status"] in BLOCKING_STATUSES)

    def record(self, entry: ApplicationRecord) -> bool:
        """Insert or update a row.

        Returns:
            ``False`` when an existing submission is left untouched.
        """
        existing = self._conn.execute(
            "SELECT status FROM applications WHERE url = ?",
            (entry.url,),
        ).fetchone()
        if existing and existing["status"] == "submitted" and entry.status != "submitted":
            return False
        self._conn.execute(
            """
            INSERT INTO applications (
                url, job_title, company, cv_used, match_score, timestamp, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                job_title = excluded.job_title,
                company = excluded.company,
                cv_used = excluded.cv_used,
                match_score = excluded.match_score,
                timestamp = excluded.timestamp,
                status = excluded.status,
                notes = excluded.notes
            """,
            (
                entry.url,
                entry.job_title,
                entry.company,
                entry.cv_used,
                entry.match_score,
                entry.timestamp,
                entry.status,
                entry.notes,
            ),
        )
        self._conn.commit()
        self.export_csv()
        return True

    def all_records(self) -> list[ApplicationRecord]:
        """Return every row, newest first."""
        rows = self._conn.execute(
            "SELECT job_title, company, url, cv_used, match_score, timestamp, status, notes "
            "FROM applications ORDER BY timestamp DESC"
        ).fetchall()
        records: list[ApplicationRecord] = []
        for row in rows:
            status = str(row["status"])
            if status not in STATUSES:
                status = "error"
            records.append(
                ApplicationRecord(
                    job_title=row["job_title"],
                    company=row["company"],
                    url=row["url"],
                    cv_used=row["cv_used"],
                    match_score=row["match_score"],
                    timestamp=row["timestamp"],
                    status=_status(status),
                    notes=row["notes"] or "",
                )
            )
        return records

    def export_csv(self) -> None:
        """Rewrite ``applications.csv`` from the current SQLite rows."""
        records = self.all_records()
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for record in records:
                writer.writerow(record.csv_row())

    def _setup(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                url TEXT PRIMARY KEY,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                cv_used TEXT NOT NULL,
                match_score REAL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()

    def _replace_from_csv(self) -> None:
        try:
            with self.csv_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
        except OSError:
            return
        self._conn.execute("DELETE FROM applications")
        for row in rows:
            url = (row.get("URL") or "").strip()
            status = (row.get("Status") or "").strip()
            if not url or status not in STATUSES:
                continue
            score_text = (row.get("Match Score") or "").strip()
            score = float(score_text) if score_text else None
            self._conn.execute(
                """
                INSERT INTO applications (
                    url, job_title, company, cv_used, match_score, timestamp, status, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    (row.get("Job Title") or "").strip(),
                    (row.get("Company") or "").strip(),
                    (row.get("CV Used") or "").strip(),
                    score,
                    (row.get("Timestamp") or utc_now()).strip(),
                    status,
                    (row.get("Notes") or "").strip(),
                ),
            )
        self._conn.commit()


def _status(value: str) -> ApplicationStatus:
    if value == "submitted":
        return "submitted"
    if value == "pending_review":
        return "pending_review"
    if value == "dry_run":
        return "dry_run"
    if value == "below_threshold":
        return "below_threshold"
    if value == "skipped_manual":
        return "skipped_manual"
    return "error"
