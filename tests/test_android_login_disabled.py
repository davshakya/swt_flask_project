import ast
import json
import secrets
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request


def functions(**overrides):
    source = Path(__file__).resolve().parents[1] / "flask_app/server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"normalize_android_sso_session_limit", "register_active_platform_session",
             "enforce_active_platform_session_limit", "active_platform_session_matches", "mobile_auth_login"}
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in selected:
        node.decorator_list = []
    settings = {"sessions": '["old", "new"]'}
    namespace = dict(DEFAULT_ANDROID_SSO_SESSION_LIMIT=1, MAX_ANDROID_SSO_SESSION_LIMIT=10,
                     SESSION_PLATFORM_ANDROID="android", secrets=secrets,
                     active_session_setting_key=lambda *a, **k: "sessions",
                     new_platform_session_id=lambda: "next",
                     get_app_setting=lambda key, default: settings.get(key, default),
                     set_app_setting=lambda key, value: settings.update({key: value}),
                     parse_active_platform_sessions=json.loads,
                     serialize_active_platform_sessions=json.dumps,
                     active_platform_sessions=lambda *a, **k: json.loads(settings["sessions"]),
                     active_platform_session_limit=lambda *a, **k: 0,
                     request=request, jsonify=jsonify, time=SimpleNamespace(sleep=lambda _: None))
    namespace.update(overrides)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace, settings


def test_zero_revokes_all_sessions_and_rejects_old_tokens():
    ns, settings = functions()
    assert ns["active_platform_session_matches"]("android", "customer", session_id="new") is False
    assert json.loads(settings["sessions"]) == []
    settings["sessions"] = '["old", "new"]'
    assert ns["enforce_active_platform_session_limit"]("android", "customer") == 2
    assert ns["register_active_platform_session"]("android", "customer") == ""
    assert json.loads(settings["sessions"]) == []


def test_reenabled_login_retains_only_configured_number():
    ns, settings = functions(active_platform_session_limit=lambda *a, **k: 1)
    assert ns["register_active_platform_session"]("android", "customer") == "next"
    assert json.loads(settings["sessions"]) == ["next"]
    assert ns["active_platform_session_matches"]("android", "customer", session_id="next")
    assert not ns["active_platform_session_matches"]("android", "customer", session_id="old")
    normalize = ns["normalize_android_sso_session_limit"]
    assert normalize(0) == 0
    assert normalize(-1) == 0
    assert normalize("invalid") == 1
    assert normalize(99) == 10


def test_disabled_customer_login_does_not_issue_token():
    issued = []
    ns, _ = functions(authenticate_dashboard_user=lambda *a: {"role": "customer", "device_id": "test"},
                      build_mobile_auth_response_payload=lambda user: issued.append(user) or {"token": "test"})
    app = Flask(__name__)
    with app.test_request_context(json={"username": "test", "password": "test"}):
        response, status = ns["mobile_auth_login"]()
        assert status == 403
        assert response.get_json()["code"] == "android_login_disabled"
    assert issued == []
    ns["active_platform_session_limit"] = lambda *a, **k: 1
    with app.test_request_context(json={"username": "test", "password": "test"}):
        assert ns["mobile_auth_login"]().get_json()["token"] == "test"
    assert len(issued) == 1
