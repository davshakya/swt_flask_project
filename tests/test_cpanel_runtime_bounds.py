from types import SimpleNamespace
from pathlib import Path
import subprocess
import sys

import pytest

from flask_app import server
from flask_app.capacity_features import CapacityFeatureRegistry


def test_cpanel_pool_default_respects_explicit_opt_out():
    assert CapacityFeatureRegistry({'SWT_CPANEL_RUNTIME': 'true'}).enabled('db_connection_pool')
    assert not CapacityFeatureRegistry({'SWT_CPANEL_RUNTIME': 'true',
                                       'FEATURE_DB_CONNECTION_POOL': 'false'}).enabled('db_connection_pool')
    assert not CapacityFeatureRegistry({}).enabled('db_connection_pool')


def test_chatbot_import_keeps_numerical_libraries_unloaded():
    result = subprocess.run([
        sys.executable, '-c',
        'import sys; import flask_app.rag_service; '
        'assert not any(name in sys.modules for name in ("sklearn", "numpy", "pandas"))'
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_cpanel_chatbot_search_stays_grounded_without_ml(tmp_path, monkeypatch):
    from flask_app import rag_service
    monkeypatch.setenv('SWT_CPANEL_RUNTIME', 'true')
    monkeypatch.delenv('RAG_TFIDF_ENABLED', raising=False)
    monkeypatch.setattr(rag_service, '_ml_import_attempted', False)
    (tmp_path / 'pump.md').write_text('Pump safety prevents overflow.', encoding='utf-8')
    results = rag_service.RagIndex([tmp_path]).search('pump safety')
    assert results and results[0].source.endswith('pump.md')
    assert rag_service._ml_import_attempted is False


def test_cpanel_startup_does_not_spawn_git(monkeypatch):
    monkeypatch.setenv('SWT_CPANEL_RUNTIME', 'true')
    for name in ('RENDER_GIT_COMMIT', 'GIT_COMMIT', 'COMMIT_SHA', 'RENDER_GIT_BRANCH',
                 'GIT_BRANCH', 'BRANCH_NAME', 'SWT_BUILD_NUMBER', 'RENDER_DEPLOY_ID',
                 'CI_PIPELINE_IID', 'CI_PIPELINE_ID', 'BUILD_NUMBER'):
        monkeypatch.delenv(name, raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('Git subprocess attempted')
    # Exceptions in detect functions are caught; record attempts separately.
    calls = []
    def record(*args, **kwargs):
        calls.append(args)
        return forbidden(*args, **kwargs)
    monkeypatch.setattr(server.subprocess, 'run', record)
    assert server.detect_git_short_commit() is None
    assert server.detect_git_branch() is None
    assert server.detect_version_sequence().isdigit()
    assert calls == []


def test_web_lock_retry_stops_after_two_attempts_but_background_keeps_budget(monkeypatch):
    monkeypatch.delenv('MYSQL_WEB_RETRY_ATTEMPTS', raising=False)
    monkeypatch.setattr(server.time, 'sleep', lambda seconds: None)
    calls = []
    def locked():
        calls.append(1)
        raise RuntimeError('Lock wait timeout exceeded')
    with server.app.test_request_context('/device/sync'):
        with pytest.raises(RuntimeError):
            server.run_with_database_lock_retries(locked, attempts=6)
    assert len(calls) == 2
    assert server.database_retry_attempts(6) == 6


def test_cpanel_socket_timeouts_are_bounded_and_configurable(monkeypatch):
    monkeypatch.setenv('SWT_CPANEL_RUNTIME', 'true')
    for name in ('MYSQL_CONNECT_TIMEOUT_SECONDS', 'MYSQL_READ_TIMEOUT_SECONDS',
                 'MYSQL_WRITE_TIMEOUT_SECONDS', 'MYSQL_LOCK_WAIT_TIMEOUT_SECONDS'):
        monkeypatch.delenv(name, raising=False)
    options = []
    queries = []
    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): queries.append(args)
    def connect(**kwargs):
        options.append(kwargs)
        return Connection()
    monkeypatch.setattr(server, 'pymysql', SimpleNamespace(connect=connect))
    monkeypatch.setattr(server, 'mysql_connection_config', lambda: {
        'host': 'localhost', 'port': 3306, 'user': 'test', 'password': 'test', 'database': 'test'})
    server._connect_mysql_unpooled_once()
    assert options[0]['connect_timeout'] == 5
    assert options[0]['read_timeout'] == options[0]['write_timeout'] == 8
    assert queries[-1][1] == (3,)
    monkeypatch.setenv('MYSQL_READ_TIMEOUT_SECONDS', '12')
    server._connect_mysql_unpooled_once()
    assert options[-1]['read_timeout'] == 12
