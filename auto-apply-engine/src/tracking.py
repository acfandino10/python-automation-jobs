"""Application log backed by SQLite, with a CSV export for spreadsheets."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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

# Statuses that mean this URL was already applied to or is waiting on the user.
BLOCKING_STATUSES = {"submitted", "pending_review"}


def _parse_timestamp(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def start_of_local_day(moment: datetime | None = None) -> datetime:
    """Local midnight. Cron days follow the machine timezone."""
    current = (moment or datetime.now()).astimezone()
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class ApplicationRecord:
    job_title: str
    company: str
    url: str
    cv_used: str
    match_score: float | None
    timestamp: str
    status: str
    notes: str = ""

    def csv_row(self) -> dict[str, str]:
        score = "" if self.match_score is None else f"{self.match_score:.0f}"
        return {
            "Job Title": self.job_title,
            "Company": self.company,
            "URL": self.url,
            "CV Used": self.cv_used,
            "Match Score": score,
            "Timestamp": self.timestamp,
            "Status": self.status,
            "Notes": self.notes,
        }


class ApplicationLog:
    """One row per job URL. A submitted application is never overwritten by a later skip."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = directory / "applications.sqlite3"
        self.csv_path = directory / "applications.csv"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._setup()
        if self.csv_path.exists():
            self._replace_from_csv()

    def close(self) -> None:
        self._conn.close()

    def is_blocked(self, url: str) -> bool:
        row = self._conn.execute("SELECT status FROM applications WHERE url = ?", (url,)).fetchone()
        return bool(row and row["status"] in BLOCKING_STATUSES)

    def submitted_since(self, start: datetime) -> int:
        """Count applications submitted at or after start. Naive datetimes are treated as UTC."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        threshold = start.astimezone(timezone.utc)
        rows = self._conn.execute(
            "SELECT timestamp FROM applications WHERE status = 'submitted'"
        ).fetchall()
        count = 0
        for row in rows:
            stamp = _parse_timestamp(row["timestamp"])
            if stamp is not None and stamp >= threshold:
                count += 1
        return count

    def record(self, entry: ApplicationRecord) -> bool:
        """Insert or update a row. Returns False when an existing submission is left untouched."""
        existing = self._conn.execute(
            "SELECT status FROM applications WHERE url = ?",
            (entry.url,),
        ).fetchone()
        if existing and existing["status"] == "submitted" and entry.status != "submitted":
            return False
        self._conn.execute(
            """
            INSERT INTO applications (url, job_title, company, cv_used, match_score, timestamp, status, notes)
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
        rows = self._conn.execute(
            "SELECT job_title, company, url, cv_used, match_score, timestamp, status, notes "
            "FROM applications ORDER BY timestamp DESC"
        ).fetchall()
        return [
            ApplicationRecord(
                job_title=row["job_title"],
                company=row["company"],
                url=row["url"],
                cv_used=row["cv_used"],
                match_score=row["match_score"],
                timestamp=row["timestamp"],
                status=row["status"],
                notes=row["notes"] or "",
            )
            for row in rows
        ]

    def export_csv(self) -> None:
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
        """The CSV is the editable copy. Deleting a row there lets that URL be tried again."""
        try:
            with self.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return
        parsed: list[tuple] = []
        for row in rows:
            url = (row.get("URL") or "").strip()
            if not url:
                continue
            score_raw = (row.get("Match Score") or "").strip()
            try:
                score = float(score_raw) if score_raw else None
            except ValueError:
                score = None
            parsed.append(
                (
                    url,
                    (row.get("Job Title") or "Unknown").strip(),
                    (row.get("Company") or "Unknown").strip(),
                    (row.get("CV Used") or "").strip(),
                    score,
                    (row.get("Timestamp") or utc_now()).strip(),
                    (row.get("Status") or "pending_review").strip(),
                    (row.get("Notes") or "").strip(),
                )
            )
        with self._conn:
            self._conn.execute("DELETE FROM applications")
            self._conn.executemany(
                """
                INSERT INTO applications (url, job_title, company, cv_used, match_score, timestamp, status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                parsed,
            )
