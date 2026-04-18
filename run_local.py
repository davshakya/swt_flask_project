import os

from flask_app import server as flask_server


app = flask_server.app


def resolve_port(default=8000):
    raw_value = os.environ.get("PORT", default)
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return int(default)


if __name__ == "__main__":
    flask_server.logger.info("Starting Smart Water Tank Server (local runner)")
    app.run(host="0.0.0.0", port=resolve_port(), threaded=True)
