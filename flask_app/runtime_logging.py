"""Request and database diagnostics without payloads, URLs, or SQL values."""
import logging
import os
import time
import uuid

from flask import g, has_request_context, request


class RuntimeContextFilter(logging.Filter):
    def filter(self, record):
        record.worker_pid = os.getpid()
        record.worker_parent_pid = os.getppid()
        record.request_id = getattr(g, 'diagnostic_request_id', '-') if has_request_context() else '-'
        return True


def install_request_diagnostics(app, logger, slow_seconds=2.0):
    @app.before_request
    def begin():
        g.diagnostic_request_id = uuid.uuid4().hex[:16]
        g.diagnostic_started_at = time.perf_counter()

    @app.after_request
    def complete(response):
        request_id = getattr(g, 'diagnostic_request_id', '-')
        response.headers['X-Request-ID'] = request_id
        duration = time.perf_counter() - getattr(g, 'diagnostic_started_at', time.perf_counter())
        if response.status_code >= 500 or duration >= slow_seconds:
            # Route templates avoid leaking tokens or IDs embedded in paths.
            route = str(request.url_rule) if request.url_rule else '<unmatched>'
            logger.log(logging.ERROR if response.status_code >= 500 else logging.WARNING,
                       'Request issue method=%s route=%s status=%s duration_ms=%.1f',
                       request.method, route, response.status_code, duration * 1000)
        return response


def log_database_operation(logger, operation, started_at, error=None, slow_seconds=1.0):
    duration = time.perf_counter() - started_at
    if error is None and duration < slow_seconds:
        return
    code = None
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        args = getattr(current, 'args', ())
        if args and isinstance(args[0], int):
            code = args[0]
            break
        current = getattr(current, '__cause__', None)
    logger.warning('Database issue operation=%s duration_ms=%.1f outcome=%s error_type=%s mysql_code=%s',
                   operation, duration * 1000, 'failed' if error else 'slow',
                   type(error).__name__ if error else '-', code if code is not None else '-')
