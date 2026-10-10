import logging
import time

from flask import Flask

from flask_app.runtime_logging import RuntimeContextFilter, install_request_diagnostics, log_database_operation


def test_request_failure_has_correlated_safe_route(caplog):
    app = Flask(__name__)
    logger = logging.getLogger('diagnostic-test')
    logger.addFilter(RuntimeContextFilter())
    install_request_diagnostics(app, logger)
    app.add_url_rule('/device/<secret>', view_func=lambda secret: ('failed', 503))
    with caplog.at_level(logging.WARNING):
        response = app.test_client().get('/device/private-token?password=private-password')
    assert response.status_code == 503
    assert len(response.headers['X-Request-ID']) == 16
    record = next(record for record in caplog.records if 'Request issue' in record.message)
    assert record.request_id == response.headers['X-Request-ID']
    assert record.worker_pid > 0 and record.worker_parent_pid > 0
    assert '/device/<secret>' in record.message
    assert 'private-token' not in caplog.text and 'private-password' not in caplog.text


def test_database_failure_reports_code_without_error_payload(caplog):
    with caplog.at_level(logging.WARNING):
        log_database_operation(logging.getLogger('database-test'), 'INSERT', time.perf_counter(),
                               RuntimeError(2006, 'private SQL credential'))
    assert 'mysql_code=2006' in caplog.text
    assert 'outcome=failed' in caplog.text
    assert 'private SQL credential' not in caplog.text


def test_fast_success_is_quiet_and_slow_success_is_logged(caplog):
    logger = logging.getLogger('database-test')
    with caplog.at_level(logging.WARNING):
        log_database_operation(logger, 'SELECT', time.perf_counter())
        assert not caplog.records
        log_database_operation(logger, 'SELECT', time.perf_counter() - 2)
    assert 'outcome=slow' in caplog.text
