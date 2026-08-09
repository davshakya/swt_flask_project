from __future__ import annotations

from datetime import datetime
import gzip
import json

from flask_app.capacity_aggregation import archive_legacy_tank_rows


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.rowcount = 0
        self.deleted_ids = []

    def execute(self, statement, parameters=()):
        if statement.lstrip().startswith("SELECT"):
            return self
        if statement.startswith("DELETE FROM tank_data"):
            self.deleted_ids = list(parameters)
            self.rowcount = len(parameters)
            return self
        raise AssertionError(statement)

    def fetchall(self):
        return self.rows


def test_legacy_archive_writes_complete_batch_before_exact_delete(tmp_path):
    rows = [
        {"id": 41, "device_id": "swt-old-1", "level": 20, "created_at": datetime(2025, 1, 1)},
        {"id": 44, "device_id": "swt-old-2", "level": 30, "created_at": datetime(2025, 1, 2)},
    ]
    cursor = FakeCursor(rows)

    result = archive_legacy_tank_rows(cursor, tmp_path, before_days=30, batch_rows=5000)

    assert result["rows"] == 2
    assert result["deleted_rows"] == 2
    assert cursor.deleted_ids == [41, 44]
    with gzip.open(result["path"], "rt", encoding="utf-8") as archive:
        archived = [json.loads(line) for line in archive]
    assert [row["id"] for row in archived] == [41, 44]
    assert not list(tmp_path.glob("*.tmp"))


def test_legacy_archive_empty_batch_is_a_noop(tmp_path):
    cursor = FakeCursor([])
    assert archive_legacy_tank_rows(cursor, tmp_path) == {
        "rows": 0,
        "deleted_rows": 0,
        "path": None,
    }
    assert cursor.deleted_ids == []
