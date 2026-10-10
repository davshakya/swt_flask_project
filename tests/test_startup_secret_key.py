"""Exercise secret resolution before database helpers are available."""

import ast
import os
from pathlib import Path

import pytest


def startup_secret_resolver():
    source = Path(__file__).resolve().parents[1] / "flask_app" / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    resolver = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_app_secret_key"
    )
    namespace = {"os": os}
    exec(compile(ast.Module(body=[resolver], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["resolve_app_secret_key"]


def test_configured_secret_resolves_without_database_helpers(monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "  stable-deployment-secret  ")
    assert startup_secret_resolver()() == ("stable-deployment-secret", "env")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_secret_reports_actionable_configuration_error(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("APP_SECRET_KEY", value)
    with pytest.raises(RuntimeError, match="APP_SECRET_KEY is missing or blank.*device.env"):
        startup_secret_resolver()()
