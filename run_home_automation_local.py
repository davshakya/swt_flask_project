import os
from pathlib import Path

from flask import Flask, redirect

from flask_app.home_automation_routes import register_home_automation_routes


PROJECT_ROOT = Path(__file__).resolve().parent
APP_ROOT = PROJECT_ROOT / "flask_app"


app = Flask(
    __name__,
    template_folder=str(APP_ROOT / "templates"),
    static_folder=str(APP_ROOT / "static"),
)
register_home_automation_routes(app)


@app.get("/")
def index():
    return redirect("/home-automation")


def resolve_port(default=5000):
    raw_value = os.environ.get("PORT", default)
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return int(default)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=resolve_port(), threaded=True)
