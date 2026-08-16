from __future__ import annotations

from datetime import date
import gzip

from flask_app.capacity_aggregation import aggregate_daily, aggregate_hourly, archive_daily_rows


class FakeCursor:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


def test_hourly_and_daily_lookbacks_are_bounded():
    cursor = FakeCursor(rowcount=7)
    assert aggregate_hourly(cursor, 9999) == 7
    assert cursor.calls[-1][1] == (168,)
    assert aggregate_daily(cursor, 9999) == 7
    assert cursor.calls[-1][1] == (90,)


def test_archive_writes_a_bounded_compressed_jsonl_batch(tmp_path):
    cursor = FakeCursor(
        rows=[
            {
                "device_id": "swt-1",
                "device_source": "real",
                "bucket_date": date(2025, 1, 1),
                "sample_count": 4,
            }
        ]
    )

    result = archive_daily_rows(cursor, tmp_path, before_days=1, batch_rows=99999)

    assert cursor.calls[0][1] == (
        30,
        "1900-01-01",
        "1900-01-01",
        "",
        "1900-01-01",
        "",
        "",
        5000,
    )
    assert result["rows"] == 1
    with gzip.open(result["path"], "rt", encoding="utf-8") as archive:
        assert '"device_id": "swt-1"' in archive.read()
    assert '"device_id": "swt-1"' in (tmp_path / ".daily-checkpoint.json").read_text()


def test_aggregation_sql_only_processes_completed_buckets():
    cursor = FakeCursor()
    aggregate_hourly(cursor)
    aggregate_daily(cursor)
    assert "recorded_at < DATE_FORMAT(CURRENT_TIMESTAMP" in cursor.calls[0][0]
    assert "%%Y-%%m-%%d %%H:00:00" in cursor.calls[0][0]
    assert "bucket_at < CURRENT_DATE" in cursor.calls[1][0]
