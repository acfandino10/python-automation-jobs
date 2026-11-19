from datetime import datetime, timedelta, timezone

from src.tracking import ApplicationLog, ApplicationRecord, start_of_local_day, utc_now


def _entry(url: str, status: str, score: float | None = 80) -> ApplicationRecord:
    return ApplicationRecord(
        job_title="Backend Engineer",
        company="Northwind",
        url=url,
        cv_used="output/tailored_cvs/backend-engineer_cv.pdf",
        match_score=score,
        timestamp=utc_now(),
        status=status,
        notes="",
    )


def test_log_blocks_submissions_and_exports_csv(tmp_path):
    log = ApplicationLog(tmp_path)
    assert log.record(_entry("https://jobs.example/1", "dry_run", 70))
    assert not log.is_blocked("https://jobs.example/1")

    assert log.record(_entry("https://jobs.example/1", "submitted", 91))
    assert log.is_blocked("https://jobs.example/1")
    assert not log.record(_entry("https://jobs.example/1", "dry_run", 10))

    log.close()
    reopened = ApplicationLog(tmp_path)
    assert reopened.is_blocked("https://jobs.example/1")
    text = (tmp_path / "applications.csv").read_text(encoding="utf-8")
    assert "Job Title,Company,URL,CV Used,Match Score,Timestamp,Status,Notes" in text
    assert "submitted" in text
    assert "https://jobs.example/1" in text
    reopened.close()


def test_deleting_a_csv_row_allows_the_url_again(tmp_path):
    log = ApplicationLog(tmp_path)
    log.record(_entry("https://jobs.example/2", "pending_review"))
    log.record(_entry("https://jobs.example/3", "below_threshold", 40))
    log.close()

    csv_path = tmp_path / "applications.csv"
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if "jobs.example/2" not in line]
    csv_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    reopened = ApplicationLog(tmp_path)
    assert not reopened.is_blocked("https://jobs.example/2")
    assert any(record.url.endswith("/3") for record in reopened.all_records())
    reopened.close()


def test_daily_count_includes_only_submissions_since_local_midnight(tmp_path):
    log = ApplicationLog(tmp_path)
    older = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0)
    log.record(_entry("https://jobs.example/old", "submitted", 90))
    log._conn.execute(
        "UPDATE applications SET timestamp = ? WHERE url = ?",
        (older.isoformat(), "https://jobs.example/old"),
    )
    log._conn.commit()
    log.export_csv()
    log.record(_entry("https://jobs.example/today", "submitted", 88))
    log.record(_entry("https://jobs.example/scored", "below_threshold", 40))

    assert log.submitted_since(start_of_local_day()) == 1
    log.close()
