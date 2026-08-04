from __future__ import annotations

from pathlib import Path


SERVER_SOURCE = Path(__file__).resolve().parents[1] / "flask_app" / "server.py"


def test_database_retention_defaults_to_30_days_and_batches_deletes():
    source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert 'DATA_RETENTION_DAYS = max(1, env_int("DATA_RETENTION_DAYS", 30))' in source
    assert "DB_RETENTION_DELETE_BATCH_ROWS" in source
    assert "def prune_delete_batches(" in source
    assert "LIMIT ?" in source


def test_database_maintenance_runs_real_table_optimization():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    start = source.index('def maybe_maintain_database(reason="periodic", pruned_rows=0, force=False):')
    body = source[start : source.index("def process_telemetry_payload", start)]

    assert "return False\n\n\ndef process_telemetry_payload" not in source
    assert "OPTIMIZE TABLE" in body
    assert "PRAGMA optimize" in body
    assert "DB_OPTIMIZE_AFTER_PRUNE_ROWS" in body
    assert '"last_action": "optimize"' in body


def test_admin_can_force_database_cleanup():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    start = source.index('@app.route("/admin/db-cleanup", methods=["POST"])')
    body = source[start : source.index('@app.route("/admin/device-source-mode"', start)]

    assert "@admin_required" in body
    assert "@csrf_protect" in body
    assert "maybe_prune_retained_rows(force=True)" in body
    assert '"pruned_rows": pruned_rows' in body
