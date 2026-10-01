"""Simulate resets against the real adapters without starting Flask."""
import ast
import logging
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from flask_app.mysql_retry import statement_allows_connection_retry


def adapters():
    source = Path(__file__).resolve().parents[1] / "flask_app" / "server.py"
    names = {"MySqlConnectionAdapter", "MySqlCursorAdapter", "mysql_exception_number",
             "mysql_exception_message", "mysql_is_connection_recoverable_error", "mysql_is_lock_error"}
    tree = ast.parse(source.read_text(encoding="utf-8"))
    module = ast.Module(body=[node for node in tree.body if
        isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names], type_ignores=[])
    namespace = dict(re=re, logger=logging.getLogger(__name__),
                     time=SimpleNamespace(sleep=lambda _delay: None),
                     statement_allows_connection_retry=statement_allows_connection_retry,
                     translate_mysql_query=lambda sql, params: (sql, params),
                     adapt_mysql_row=lambda row: row)
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


class Connection:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.error = None
        self.lastrowid = 1
        self.rowcount = 1

    def cursor(self):
        return self

    def execute(self, sql, _params):
        self.calls.append(sql)
        if self.error:
            raise self.error

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_read_reset_after_write_does_not_silently_drop_transaction():
    namespace = adapters()
    connection = Connection()
    adapter = namespace["MySqlConnectionAdapter"](connection)
    namespace["connect_mysql"] = lambda: pytest.fail("must not reconnect a write transaction")
    adapter.execute("INSERT INTO example VALUES (1)")
    connection.error = Exception(2006, "MySQL server has gone away")
    with pytest.raises(Exception, match="gone away"):
        adapter.execute("SELECT * FROM example")
    assert connection.calls == ["INSERT INTO example VALUES (1)", "SELECT * FROM example"]


def test_plain_read_reset_can_reconnect_and_recover():
    namespace = adapters()
    failed, replacement = Connection(), Connection()
    failed.error = Exception(2006, "MySQL server has gone away")
    adapter = namespace["MySqlConnectionAdapter"](failed)
    namespace["connect_mysql"] = lambda: namespace["MySqlConnectionAdapter"](replacement)
    assert adapter.execute("SELECT * FROM example").rowcount == 1
    assert failed.closed
    assert replacement.calls == ["SELECT * FROM example"]


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_completed_transaction_allows_subsequent_read_recovery(finish):
    namespace = adapters()
    adapter = namespace["MySqlConnectionAdapter"](Connection())
    adapter.execute("UPDATE example SET value=1")
    assert adapter.transaction_has_writes
    getattr(adapter, finish)()
    assert not adapter.transaction_has_writes


def test_failed_pooled_reconnect_releases_lease_only_once():
    namespace = adapters()
    released = []
    def acquire():
        raise RuntimeError("pool unavailable")
    pool = SimpleNamespace(release=lambda *args, **kwargs: released.append((args, kwargs)),
                           acquire=acquire)
    connection = Connection()
    adapter = namespace["MySqlConnectionAdapter"](connection, pool=pool, pool_created_at=123)
    with pytest.raises(RuntimeError, match="pool unavailable"):
        with adapter:
            adapter.reconnect()
    assert len(released) == 1
    assert released[0][1] == {"discard": True}
