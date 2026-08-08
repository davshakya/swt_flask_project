import os


def env_flag(name, default=False):
    raw_value = os.environ.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default

    text = str(raw_value).strip()
    if not text:
        return default

    try:
        return int(text)
    except (TypeError, ValueError):
        return default


IS_RENDER = bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))
bind = f"0.0.0.0:{env_int('PORT', 8000)}"
workers = env_int("WEB_CONCURRENCY", 1)
threads = env_int("GUNICORN_THREADS", 2 if IS_RENDER else 4)
timeout = env_int("GUNICORN_TIMEOUT", 120)
graceful_timeout = env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = env_int("GUNICORN_KEEPALIVE", 5)
max_requests = env_int("GUNICORN_MAX_REQUESTS", 250 if IS_RENDER else 0)
max_requests_jitter = env_int("GUNICORN_MAX_REQUESTS_JITTER", 25 if max_requests else 0)
loglevel = os.environ.get("LOG_LEVEL", "info")
accesslog = "-" if env_flag("GUNICORN_ACCESS_LOG_ENABLED", default=not IS_RENDER) else None
errorlog = "-"
_color_logs = env_flag("SWT_LOG_COLOR", default=False)
_cyan = "\033[36m" if _color_logs else ""
_green = "\033[32m" if _color_logs else ""
_reset = "\033[0m" if _color_logs else ""
access_log_format = f'{_cyan}%(t)s{_reset} {_green}[INFO]{_reset} HTTP client=%(h)s request="%(r)s" status=%(s)s bytes=%(b)s duration=%(L)ss user_agent="%(a)s"'
error_log_format = f"{_cyan}[%(asctime)s]{_reset} [%(levelname)s] %(message)s"
