import threading

import pytest
from flask import Flask

from flask_app.mysql_circuit import MySqlConnectionCircuit
from flask_app.database_availability import install_database_error_handlers


def test_failed_connections_open_cooldown_and_successful_probe_recovers():
    now = [100.0]
    circuit = MySqlConnectionCircuit(clock=lambda: now[0])
    calls = []

    def fail():
        calls.append(1)
        raise RuntimeError('disconnect')

    for _ in range(3):
        with pytest.raises(RuntimeError):
            circuit.run(fail, lambda exc: True)
    for _ in range(10):
        with pytest.raises(TimeoutError, match='cooldown'):
            circuit.run(fail, lambda exc: True)
    assert len(calls) == 3
    now[0] += 5
    assert circuit.run(lambda: 'connected', lambda exc: True) == 'connected'
    assert circuit.run(lambda: 'next connection', lambda exc: True) == 'next connection'


def test_configuration_errors_do_not_open_circuit():
    circuit = MySqlConnectionCircuit()
    for _ in range(5):
        with pytest.raises(ValueError, match='credentials'):
            circuit.run(lambda: (_ for _ in ()).throw(ValueError('credentials')),
                        lambda exc: False)
    assert circuit.run(lambda: 'fixed', lambda exc: False) == 'fixed'


def test_only_one_recovery_probe_can_run():
    now = [100.0]
    circuit = MySqlConnectionCircuit(failure_limit=1, clock=lambda: now[0])
    with pytest.raises(RuntimeError):
        circuit.run(lambda: (_ for _ in ()).throw(RuntimeError('disconnect')), lambda exc: True)
    now[0] += 5
    entered, release = threading.Event(), threading.Event()
    results = []

    def probe():
        entered.set()
        assert release.wait(5)
        return 'connected'

    worker = threading.Thread(target=lambda: results.append(circuit.run(probe, lambda exc: True)))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(TimeoutError, match='cooldown'):
            circuit.run(lambda: pytest.fail('second probe'), lambda exc: True)
    finally:
        release.set()
        worker.join(5)
    assert results == ['connected']


def test_failed_recovery_probe_starts_new_cooldown():
    now = [100.0]
    circuit = MySqlConnectionCircuit(failure_limit=1, clock=lambda: now[0])

    def fail():
        raise RuntimeError('disconnect')

    with pytest.raises(RuntimeError):
        circuit.run(fail, lambda exc: True)
    now[0] += 5
    with pytest.raises(RuntimeError):
        circuit.run(fail, lambda exc: True)
    with pytest.raises(TimeoutError):
        circuit.run(lambda: 'connected', lambda exc: True)
    now[0] += 5
    assert circuit.run(lambda: 'connected', lambda exc: True) == 'connected'


def test_cooldown_returns_retryable_503_without_exposing_database_details():
    import logging
    app = Flask(__name__)
    install_database_error_handlers(app, None, logging.getLogger(__name__))

    @app.get('/test')
    def route():
        raise TimeoutError('MySQL connection recovery cooldown; please retry')

    response = app.test_client().get('/test')
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '5'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json['code'] == 'database_unavailable'


def test_real_connection_entrypoint_uses_circuit_before_opening_database():
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    source = Path(__file__).resolve().parents[1] / 'flask_app' / 'server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'connect_mysql')
    calls = []

    def fail():
        calls.append(1)
        raise RuntimeError('disconnect')

    namespace = {
        'CAPACITY_FEATURES': SimpleNamespace(enabled=lambda feature: False),
        'connect_mysql_unpooled': fail,
        '_MYSQL_CONNECTION_CIRCUIT': MySqlConnectionCircuit(),
        'mysql_is_connection_recoverable_error': lambda exc: True,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            namespace['connect_mysql']()
    with pytest.raises(TimeoutError, match='cooldown'):
        namespace['connect_mysql']()
    assert len(calls) == 3
