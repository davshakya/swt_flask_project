"""Exercise the production compatibility helper without booting Flask."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def retry_namespace():
    source = Path(__file__).resolve().parents[1] / 'server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == '_run_with_database_lock_retries')
    delays, jitter_limits = [], []

    def jitter(low, high):
        jitter_limits.append((low, high))
        return high

    namespace = {
        'time': SimpleNamespace(sleep=delays.append),
        'random': SimpleNamespace(uniform=jitter),
        'flask_server': SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: None)),
        '_database_is_locked_error': lambda exc: str(exc) == 'lock timeout',
        '_database_connection_recoverable_error': lambda exc: str(exc) == 'disconnect',
    }
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace, delays, jitter_limits


def test_production_lock_retries_use_exponential_backoff_with_bounded_jitter():
    namespace, delays, jitter_limits = retry_namespace()
    calls = []

    def operation():
        calls.append(1)
        if len(calls) < 4:
            raise RuntimeError('lock timeout')
        return 'saved'

    assert namespace['_run_with_database_lock_retries'](
        operation, attempts=4, initial_delay_s=0.5) == 'saved'
    assert delays == [0.625, 1.25, 2.25]
    assert jitter_limits == [(0.0, 0.125), (0.0, 0.25), (0.0, 0.25)]


def test_production_does_not_replay_disconnect_without_opt_in():
    namespace, delays, _ = retry_namespace()
    calls = []

    def operation():
        calls.append(1)
        raise RuntimeError('disconnect')

    with pytest.raises(RuntimeError, match='disconnect'):
        namespace['_run_with_database_lock_retries'](operation, attempts=3)
    assert len(calls) == 1
    assert delays == []
