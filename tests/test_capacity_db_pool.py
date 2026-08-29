from __future__ import annotations

from flask_app.capacity_db_pool import MySqlConnectionPool


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.pings = 0
        self.rollbacks = 0

    def ping(self, reconnect=False):
        assert reconnect is False
        self.pings += 1
        if self.closed:
            raise RuntimeError("stale")

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_pool_reuses_connection_with_pre_ping():
    created = []

    def factory():
        connection = FakeConnection()
        created.append(connection)
        return connection

    pool = MySqlConnectionPool(factory, size=2, max_overflow=1, recycle_seconds=240)
    connection, created_at = pool.acquire()
    pool.release(connection, created_at)
    reused, reused_at = pool.acquire()

    assert reused is connection
    assert reused.pings == 1
    pool.release(reused, reused_at)
    assert pool.stats() == {
        "size": 2,
        "max_overflow": 1,
        "total": 1,
        "idle": 1,
        "created": 1,
        "recycled": 0,
    }


def test_pool_limits_retained_connections_and_closes_overflow():
    pool = MySqlConnectionPool(FakeConnection, size=2, max_overflow=1)
    leases = [pool.acquire() for _ in range(3)]
    for connection, created_at in leases:
        pool.release(connection, created_at)

    assert pool.stats()["idle"] == 2
    assert pool.stats()["total"] == 2
    assert sum(connection.closed for connection, _ in leases) == 1


def test_pool_discards_stale_connection_and_replaces_it():
    created = []

    def factory():
        connection = FakeConnection()
        created.append(connection)
        return connection

    pool = MySqlConnectionPool(factory)
    stale, created_at = pool.acquire()
    pool.release(stale, created_at)
    stale.closed = True

    replacement, replacement_at = pool.acquire()

    assert replacement is not stale
    assert pool.stats()["recycled"] == 1
    assert pool.stats()["created"] == 2
    pool.release(replacement, replacement_at)


def test_server_pool_integration_is_feature_gated_and_reports_stats():
    from flask_app import server

    source = (server.PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    assert 'CAPACITY_FEATURES.enabled("db_connection_pool")' in source
    assert '"pool_stats": _MYSQL_CONNECTION_POOL.stats()' in source
    assert "return connect_mysql_unpooled()" in source


def test_idempotent_database_retry_recovers_from_mysql_disconnect(monkeypatch):
    from flask_app import server

    attempts = []
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(server.random, "uniform", lambda _start, _end: 0.0)

    def operation():
        attempts.append(1)
        if len(attempts) == 1:
            raise server.pymysql.err.OperationalError(2006, "MySQL server has gone away")
        return "persisted"

    result = server.run_with_database_lock_retries(
        operation,
        operation_name="test idempotent upsert",
        attempts=3,
        initial_delay_s=0.01,
        retry_connection_errors=True,
    )

    assert result == "persisted"
    assert len(attempts) == 2


def test_database_retry_does_not_replay_disconnect_by_default():
    from flask_app import server

    attempts = []

    def operation():
        attempts.append(1)
        raise server.pymysql.err.OperationalError(2006, "MySQL server has gone away")

    try:
        server.run_with_database_lock_retries(operation, attempts=3, initial_delay_s=0)
    except server.pymysql.err.OperationalError:
        pass
    else:
        raise AssertionError("connection error should propagate for a non-idempotent operation")

    assert len(attempts) == 1
