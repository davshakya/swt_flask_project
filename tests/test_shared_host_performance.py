"""Database-free regression checks for shared-host resource bounds."""
import ast
from pathlib import Path
from types import SimpleNamespace

from flask_app.capacity_db_pool import MySqlConnectionPool


class Connection:
    def __init__(self):
        self.closed = False

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_expired_lease_is_not_retained(monkeypatch):
    from flask_app import capacity_db_pool
    clock = [100.0]
    monkeypatch.setattr(capacity_db_pool.time, "monotonic", lambda: clock[0])
    pool = MySqlConnectionPool(Connection, recycle_seconds=30)
    conn, created = pool.acquire()
    clock[0] = 131.0
    pool.release(conn, created)
    assert conn.closed
    assert pool.stats()["total"] == 0


def test_fork_resets_pool_without_sending_mysql_quit():
    closed = []
    conn = Connection()
    conn._sock = SimpleNamespace(close=lambda: closed.append("descriptor"))
    pool = MySqlConnectionPool(lambda: conn)
    lease = pool.acquire()
    pool.release(*lease)
    original_condition = pool._condition
    pool._after_fork()
    assert closed == ["descriptor"]
    assert conn.closed is False
    assert pool._condition is not original_condition
    assert pool.stats()["total"] == pool.stats()["idle"] == 0


def test_snapshot_cache_stays_bounded_and_reuses_recent_reads():
    source = Path(__file__).resolve().parents[1] / "flask_app" / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "load_dashboard_snapshot")
    reads = []
    def fetch(device):
        reads.append(device)
        return {"device_id": device}
    import time
    state = {"normalize_device_id": lambda x: x, "get_device_source_mode": lambda: "real",
             "SNAPSHOT_CACHE_TTL_SECONDS": 2, "SNAPSHOT_CACHE_MAX_ENTRIES": 2,
             "dashboard_snapshot_cache": {}, "time": time, "fetch_device_snapshot": fetch}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), state)
    load = state["load_dashboard_snapshot"]
    for device in ("a", "b", "c", "c"):
        assert load(device) == {"device_id": device}
    assert reads == ["a", "b", "c"]
    assert len(state["dashboard_snapshot_cache"]) == 2
