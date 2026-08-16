from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path

from flask_app.capacity_operations import fetch_runtime_status, mysql_database_usage, record_runtime_status


BACKUP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record_capacity_backup.py"


class Cursor:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def test_runtime_status_round_trip_contract():
    cursor = Cursor(rows=[{"status_key": "cron:hourly", "status_value": "ok", "details_json": '{"rows":2}', "checked_at": "now"}])
    record_runtime_status(cursor, "cron:hourly", "ok", {"rows": 2})
    assert cursor.calls[0][1] == ("cron:hourly", "ok", '{"rows":2}')
    assert fetch_runtime_status(cursor)["cron:hourly"]["details"] == {"rows": 2}


def test_database_quota_warning_starts_at_seventy_percent():
    cursor = Cursor(row={"total_bytes": 75 * 1024 * 1024})
    usage = mysql_database_usage(cursor, quota_mb=100)
    assert usage["usage_percent"] == 75.0
    assert usage["warning"] is True


def test_backup_validator_checks_gzip_and_checksum(tmp_path):
    spec = importlib.util.spec_from_file_location("capacity_backup", BACKUP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backup = tmp_path / "swt.sql.gz"
    with gzip.open(backup, "wb") as archive:
        archive.write(b"CREATE TABLE phase13(id INT);\nINSERT INTO phase13 VALUES (1);\n")
    digest = module.sha256_file(backup)
    Path(f"{backup}.sha256").write_text(f"{digest}  {backup.name}\n", encoding="utf-8")

    details = module.validate_backup(backup)
    assert details["sha256"] == digest
    assert details["bytes"] > 0
    assert details["uncompressed_bytes"] > 0


def test_backup_validator_rejects_empty_but_valid_gzip(tmp_path):
    spec = importlib.util.spec_from_file_location("capacity_backup_empty", BACKUP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backup = tmp_path / "empty.sql.gz"
    with gzip.open(backup, "wb"):
        pass

    try:
        module.validate_backup(backup)
    except ValueError as exc:
        assert "non-empty SQL dump" in str(exc)
    else:
        raise AssertionError("empty gzip backup was accepted")


def test_system_status_exposes_operations_only_behind_capacity_flags():
    source = (BACKUP_SCRIPT.parents[1] / "flask_app" / "server.py").read_text(encoding="utf-8")
    assert '("cron_health", "database_size_alerts", "offserver_backup")' in source
    assert 'payload["capacity"]["operations"]' in source
