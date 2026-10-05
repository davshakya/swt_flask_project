import logging

from flask import Flask, abort
import pymysql
import pytest

from flask_app.database_availability import install_database_error_handlers


@pytest.mark.parametrize("error", [
    pymysql.err.OperationalError(2006, "MySQL server has gone away"),
    pymysql.err.OperationalError(2003, "Connection refused"),
    pymysql.err.OperationalError(1205, "Lock wait timeout exceeded"),
    TimeoutError("MySQL connection pool exhausted"),
])
def test_database_outage_returns_retryable_response_and_worker_remains_usable(error):
    app = Flask(__name__)
    app.testing = True
    install_database_error_handlers(app, pymysql.err.OperationalError, logging.getLogger(__name__))

    @app.get("/fail")
    def fail():
        raise error

    @app.get("/live")
    def live():
        return {"ok": True}

    client = app.test_client()
    response = client.get("/fail")
    assert response.status_code == 503
    assert response.json["code"] == "database_unavailable"
    assert response.headers["Retry-After"] == "5"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/live").status_code == 200


def test_wrapped_connection_failure_is_handled_without_exposing_credentials():
    app = Flask(__name__)
    install_database_error_handlers(app, pymysql.err.OperationalError, logging.getLogger(__name__))

    @app.get("/")
    def fail():
        try:
            raise pymysql.err.OperationalError(2003, "Connection refused")
        except Exception as error:
            raise RuntimeError("private connection configuration") from error

    response = app.test_client().get("/")
    assert response.status_code == 503
    assert b"private" not in response.data


def test_non_database_errors_and_http_errors_keep_correct_status():
    app = Flask(__name__)
    install_database_error_handlers(app, pymysql.err.OperationalError, logging.getLogger(__name__))

    @app.get("/bug")
    def bug():
        raise RuntimeError("private bug details")

    @app.get("/auth")
    def auth():
        abort(401)

    client = app.test_client()
    response = client.get("/bug")
    assert response.status_code == 500
    assert b"private bug details" not in response.data
    assert client.get("/auth").status_code == 401
    assert client.get("/missing").status_code == 404


def test_event_batches_commit_before_next_chunk_and_retry_only_failed_chunk(monkeypatch):
    from flask_app import server
    monkeypatch.setenv("DEVICE_EVENT_WRITE_BATCH_ROWS", "2")
    monkeypatch.setattr(server.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(server.random, "uniform", lambda *_args: 0.0)
    committed = []
    batches = []
    scheduled = []
    active = [False]

    class Database:
        def __init__(self):
            self.keys = []

        def __enter__(self):
            assert not active[0], "transactions must not overlap"
            active[0] = True
            batches.append(self.keys)
            return self

        def execute(self, _sql, params):
            self.keys.append(params[0])
            if len(batches) == 2:
                raise pymysql.err.OperationalError(1213, "Deadlock found when trying to get lock")

        def __exit__(self, error_type, *_args):
            active[0] = False
            if error_type is None:
                committed.extend(self.keys)
            return False

    monkeypatch.setattr(server, "get_db", Database)
    monkeypatch.setattr(server, "schedule_dashboard_summary_refresh", scheduled.append)
    events = [{"kind": "test", "message": "event", "details": {"event_key": str(i)}} for i in range(5)]
    assert server.persist_device_events(events, "swt-batch-test-001") == 5
    assert [len(batch) for batch in batches] == [2, 1, 2, 1]
    assert len(set(committed)) == 5
    assert batches[1][0] == batches[2][0]
    assert scheduled == ["swt-batch-test-001"]
