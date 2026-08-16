from __future__ import annotations

from flask_app.capacity_features import CapacityFeatureRegistry
from flask_app.capacity_schema import CAPACITY_SCHEMA_VERSION, CAPACITY_TABLES, ensure_capacity_schema
from flask_app import server as server_module


class RecordingCursor:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), tuple(params or ())))
        return self


def test_capacity_schema_contains_all_phase_one_tables():
    cursor = RecordingCursor()

    ensure_capacity_schema(cursor)

    sql = "\n".join(statement for statement, _params in cursor.statements)
    for table_name in CAPACITY_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in sql
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in sql
    assert cursor.statements[-1][1] == (CAPACITY_SCHEMA_VERSION,)


def test_capacity_schema_statements_are_idempotent():
    cursor = RecordingCursor()

    ensure_capacity_schema(cursor)
    first_run = list(cursor.statements)
    ensure_capacity_schema(cursor)

    assert cursor.statements == first_run + first_run
    assert all(
        "CREATE TABLE IF NOT EXISTS" in statement or "ON CONFLICT(version) DO NOTHING" in statement
        for statement, _params in first_run
    )


def test_capacity_writes_require_schema_flag():
    registry = CapacityFeatureRegistry(environ={"FEATURE_LATEST_STATE_WRITES": "true"})

    assert registry.snapshot()["latest_state_writes"]["configured"] is True
    assert registry.enabled("latest_state_writes") is False
    assert "FEATURE_CAPACITY_SCHEMA" in registry.warnings[0]


def test_capacity_schema_and_write_chain_can_be_enabled():
    registry = CapacityFeatureRegistry(
        environ={
            "FEATURE_CAPACITY_SCHEMA": "true",
            "FEATURE_LATEST_STATE_WRITES": "true",
            "FEATURE_NARROW_HISTORY_WRITES": "true",
        }
    )

    assert registry.enabled("capacity_schema") is True
    assert registry.enabled("latest_state_writes") is True
    assert registry.enabled("narrow_history_writes") is True
    assert registry.warnings == ()


def test_capacity_schema_creates_tables_in_mysql():
    with server_module.get_db() as db:
        cursor = db.cursor()
        ensure_capacity_schema(cursor)
        table_names = set(server_module.list_database_table_names(cursor))
        migration = cursor.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            (CAPACITY_SCHEMA_VERSION,),
        ).fetchone()

    assert set(CAPACITY_TABLES).issubset(table_names)
    assert migration["version"] == CAPACITY_SCHEMA_VERSION
