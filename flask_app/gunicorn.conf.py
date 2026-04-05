import os

from flask_app.runtime_utils import env_flag, env_int


bind = f"0.0.0.0:{env_int('PORT', 8000)}"
workers = env_int("WEB_CONCURRENCY", 1)
threads = env_int("GUNICORN_THREADS", 4)
timeout = env_int("GUNICORN_TIMEOUT", 120)
graceful_timeout = env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = env_int("GUNICORN_KEEPALIVE", 5)
loglevel = os.environ.get("LOG_LEVEL", "info")
accesslog = "-" if env_flag("GUNICORN_ACCESS_LOG_ENABLED", default=not bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))) else None
errorlog = "-"
