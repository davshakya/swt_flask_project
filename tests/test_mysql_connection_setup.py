"""Exercise connection setup without booting Flask or touching a database."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


def setup_namespace(connect):
    source = Path(__file__).resolve().parents[1] / "flask_app" / "server.py"
    names = {
        "connect_mysql_unpooled", "_connect_mysql_unpooled_once",
        "mysql_exception_number", "mysql_exception_message",
        "mysql_is_connection_recoverable_error",
        "queue_device_command",
        "run_with_database_lock_retries", "mysql_is_lock_error", "database_is_locked_error",
    }
    tree = ast.parse(source.read_text(encoding="utf-8"))
    module = ast.Module(body=[node for node in tree.body
                             if isinstance(node, ast.FunctionDef) and node.name in names],
                        type_ignores=[])
    namespace = {
        "pymysql": SimpleNamespace(connect=connect),
        "mysql_connection_config": lambda: dict(host="localhost", port=3306,
                                                user="test", password="test", database="test"),
        "_MYSQL_RESOLVED_LOCAL_PORT": None,
        "_MYSQL_RESOLVED_UNIX_SOCKET": None,
        "discover_local_mysql_socket": lambda _config: None,
        "os": SimpleNamespace(environ={}),
        "env_int": lambda _name, default: default,
        "MySqlDictCursor": object,
        "MySqlConnectionAdapter": lambda conn: conn,
        "logger": logging.getLogger(__name__),
        "time": SimpleNamespace(sleep=lambda _delay: None),
        "random": SimpleNamespace(uniform=lambda *_args: 0.0),
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


class Connection:
    def __init__(self, setup_error=None):
        self.setup_error = setup_error
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        if self.setup_error:
            raise self.setup_error

    def close(self):
        self.closed = True


def test_connection_reset_during_handshake_recovers_before_application_work():
    calls = []
    connection = Connection()

    def connect(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Exception(2003, "Connection reset by peer")
        return connection

    namespace = setup_namespace(connect)
    assert namespace["connect_mysql_unpooled"]() is connection
    assert len(calls) == 2


def test_local_socket_is_preferred_and_reused_after_transient_reset():
    calls = []
    connection = Connection()

    def connect(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise Exception(2003, "Connection reset by peer")
        return connection

    namespace = setup_namespace(connect)
    namespace["discover_local_mysql_socket"] = lambda _config: "/run/mysqld/mysqld.sock"
    assert namespace["connect_mysql_unpooled"]() is connection
    assert calls[0]["unix_socket"] == "/run/mysqld/mysqld.sock"
    assert calls[1]["unix_socket"] == "/run/mysqld/mysqld.sock"
    namespace["connect_mysql_unpooled"]()
    assert calls[2]["unix_socket"] == calls[1]["unix_socket"]


def test_explicit_socket_is_preserved_during_retry():
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise Exception(2006, "MySQL server has gone away")
        return Connection()

    namespace = setup_namespace(connect)
    namespace["os"].environ["MYSQL_UNIX_SOCKET"] = "/custom/mysql.sock"
    namespace["connect_mysql_unpooled"]()
    assert [call["unix_socket"] for call in calls] == ["/custom/mysql.sock"] * 2


def test_failed_socket_recovery_does_not_cache_unverified_transport():
    namespace = setup_namespace(lambda **_kwargs: (_ for _ in ()).throw(
        Exception(2006, "MySQL server has gone away")))
    namespace["discover_local_mysql_socket"] = lambda _config: "/run/mysqld/mysqld.sock"
    with pytest.raises(RuntimeError):
        namespace["connect_mysql_unpooled"]()
    assert namespace["_MYSQL_RESOLVED_UNIX_SOCKET"] is None


def test_disappeared_cached_socket_recovers_using_original_tcp_configuration():
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        if "unix_socket" in kwargs:
            raise Exception(2003, "Can't connect: socket file missing")
        return Connection()

    namespace = setup_namespace(connect)
    namespace["_MYSQL_RESOLVED_UNIX_SOCKET"] = "/run/mysqld/mysqld.sock"
    namespace["connect_mysql_unpooled"]()
    assert len(calls) == 2
    assert "unix_socket" not in calls[1]
    assert namespace["_MYSQL_RESOLVED_UNIX_SOCKET"] is None


def test_session_setup_reset_closes_failed_connection_and_recovers():
    failed = Connection(Exception(2006, "MySQL server has gone away"))
    replacement = Connection()
    connections = iter([failed, replacement])
    namespace = setup_namespace(lambda **_kwargs: next(connections))
    assert namespace["connect_mysql_unpooled"]() is replacement
    assert failed.closed
    assert not replacement.closed


@pytest.mark.parametrize("code,message,expected_calls", [
    (2006, "MySQL server has gone away", 2),
    (1045, "Access denied", 1),
    (2003, "Connection refused", 1),
])
def test_connection_retry_is_bounded_and_does_not_retry_configuration_errors(code, message, expected_calls):
    calls = []

    def connect(**_kwargs):
        calls.append(1)
        raise Exception(code, message)

    namespace = setup_namespace(connect)
    with pytest.raises(RuntimeError, match="MySQL connection failed"):
        namespace["connect_mysql_unpooled"]()
    assert len(calls) == expected_calls


def test_non_transient_session_setup_error_closes_connection_without_retry():
    connection = Connection(Exception(1227, "Access denied for session setting"))
    calls = []

    def connect(**_kwargs):
        calls.append(1)
        return connection

    namespace = setup_namespace(connect)
    with pytest.raises(Exception, match="Access denied for session setting"):
        namespace["connect_mysql_unpooled"]()
    assert connection.closed
    assert len(calls) == 1


def test_queue_retry_keeps_request_id_and_retries_whole_transaction():
    namespace = setup_namespace(None)
    calls = []
    retry_options = {}
    namespace["secrets"] = SimpleNamespace(token_hex=lambda _size: "stable-id")

    def queue_once(command, target, **kwargs):
        calls.append((command, target, kwargs))
        if len(calls) == 1:
            raise Exception(1213, "Deadlock")
        return 42

    def retry(operation, **options):
        retry_options.update(options)
        try:
            return operation()
        except Exception:
            return operation()

    namespace["_queue_device_command_once"] = queue_once
    namespace["run_with_database_lock_retries"] = retry
    assert namespace["queue_device_command"]("OFF", "device", expires_in_seconds=600) == 42
    assert calls[0] == calls[1]
    assert calls[0][2] == {"request_id": "stable-id", "expires_in_seconds": 600}
    assert retry_options["attempts"] == 3
    assert retry_options["retry_connection_errors"] is False


@pytest.mark.parametrize("code,expected_calls", [(1213, 3), (2006, 1)])
def test_queue_actual_retry_bounds_deadlocks_and_does_not_replay_disconnects(code, expected_calls):
    namespace = setup_namespace(None)
    namespace["secrets"] = SimpleNamespace(token_hex=lambda _size: "stable-id")
    calls = []

    def queue_once(*_args, **_kwargs):
        calls.append(1)
        raise Exception(code, "Deadlock" if code == 1213 else "MySQL server has gone away")

    namespace["_queue_device_command_once"] = queue_once
    with pytest.raises(Exception):
        namespace["queue_device_command"]("OFF", "device")
    assert len(calls) == expected_calls
