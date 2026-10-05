"""Keep database outages from becoming unhandled request exceptions."""
from flask import jsonify
from werkzeug.exceptions import InternalServerError


def is_transient_database_failure(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        args = getattr(error, "args", ())
        if args and isinstance(args[0], int) and args[0] in {1205, 1213, 2003, 2006, 2013, 2055}:
            return True
        message = str(error).lower()
        if any(text in message for text in (
            "mysql server has gone away", "lost connection to mysql",
            "mysql connection pool exhausted", "lock wait timeout exceeded",
            "mysql connection recovery cooldown",
            "deadlock found when trying to get lock",
        )):
            return True
        error = getattr(error, "__cause__", None)
    return False


def install_database_error_handlers(app, database_error_class, logger):
    def handle(error):
        if not is_transient_database_failure(error):
            logger.exception("Unexpected request failure")
            return InternalServerError()
        logger.warning("Database temporarily unavailable for request: %s", error)
        response = jsonify({"ok": False, "error": "Database temporarily unavailable. Please retry.",
                            "code": "database_unavailable"})
        response.status_code = 503
        response.headers["Retry-After"] = "5"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.register_error_handler(RuntimeError, handle)
    app.register_error_handler(TimeoutError, handle)
    if database_error_class is not None:
        app.register_error_handler(database_error_class, handle)
