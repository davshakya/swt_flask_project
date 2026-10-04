"""Exercise the real routes and persistence without starting the production DB."""
import ast
from functools import wraps
from pathlib import Path
import secrets
import sqlite3
from types import SimpleNamespace

from flask import Flask, abort, redirect, request, session
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "flask_app/server.py"


@pytest.fixture
def portal():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    app = Flask(__name__)
    app.secret_key = "test-only"
    names = {"ensure_customer_accounts_table", "ensure_customer_accounts_columns",
             "fetch_customer_account", "customer_web_login_enabled", "customer_auth_marker",
             "current_auth_marker_for_identity", "current_session_auth_marker",
             "stored_dashboard_identity_is_valid", "handle_role_login", "admin_required",
             "csrf_protect", "validate_csrf_or_abort", "admin_device_detail_customer_web_login"}
    ns = dict(app=app, get_db=lambda: db, normalize_device_id=lambda v: str(v or "").strip(),
              session=session, request=request, secrets=secrets, wraps=wraps, abort=abort,
              redirect=redirect, url_for=lambda name, **kw: "/" + name,
              current_scope_device_id=lambda v: v, current_actor_username=lambda: "admin",
              log_audit_event=lambda **kw: None, activate_dashboard_identity=lambda role: session.get("role") == role,
              is_admin_user=lambda: session.get("role") == "admin", role_mismatch_response=lambda: abort(403),
              dashboard_home_url=lambda role=None: "/dashboard", resolve_next_url=lambda default: default,
              authenticate_dashboard_user=lambda u, p: {"role": "customer", "device_id": u, "username": u} if p == "correct" else None,
              store_dashboard_identity=lambda user: session.update(logged_in=True, role="customer"),
              can_access_next_url=lambda *a: True, logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
              time=SimpleNamespace(sleep=lambda *a: None), render_login_page=lambda **kw: kw.get("error") or "Login",
              LOGIN_USERNAME="admin", current_dashboard_auth_marker=lambda: "admin-marker",
              build_auth_marker=lambda *args: repr(args), current_mobile_session_epoch=lambda device: "epoch",
              dashboard_identity_prefix=lambda role: role, SESSION_PLATFORM_DASHBOARD="dashboard",
              active_platform_session_matches=lambda *args, **kw: True)
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), ns)
    ns["ensure_customer_accounts_table"](db)
    ns["ensure_customer_accounts_columns"](db)
    db.execute("INSERT INTO customer_accounts(device_id,password_hash) VALUES ('tank-a','hash'), ('tank-b','hash')")
    app.add_url_rule("/login/customer", "customer_login", lambda: ns["handle_role_login"]("customer"), methods=["GET", "POST"])
    yield app, db, ns
    db.close()


def test_admin_toggle_persists_and_scopes_website_access(portal):
    app, db, ns = portal
    mobile_marker = ns["customer_auth_marker"]("tank-a")
    client = app.test_client()
    with client.session_transaction() as cookie:
        cookie.update(role="admin", csrf_token="csrf")
    response = client.post("/devices/tank-a/customer-web-login", data={"csrf_token": "csrf", "web_login_enabled": "0"})
    assert response.status_code == 302
    assert not ns["customer_web_login_enabled"]("tank-a")
    assert ns["customer_web_login_enabled"]("tank-b")
    assert ns["customer_auth_marker"]("tank-a") == mobile_marker
    visitor = app.test_client()
    response = visitor.post("/login/customer", data={"username": "tank-a", "password": "correct"})
    assert response.status_code == 200 and b"Invalid username or password" in response.data
    with app.test_request_context():
        session.update(logged_in=True, role="customer", device_id="tank-a", customer_logged_in=True, customer_device_id="tank-a")
        assert ns["current_session_auth_marker"]() is None
        assert not ns["stored_dashboard_identity_is_valid"]("customer")
    client.post("/devices/tank-a/customer-web-login", data={"csrf_token": "csrf", "web_login_enabled": "1"})
    assert visitor.post("/login/customer", data={"username": "tank-a", "password": "correct"}).status_code == 302


def test_toggle_requires_admin_csrf_and_valid_choice(portal):
    app, db, ns = portal
    client = app.test_client()
    url = "/devices/tank-a/customer-web-login"
    assert client.post(url, data={"web_login_enabled": "0"}).location.endswith("admin_login")
    with client.session_transaction() as cookie:
        cookie.update(role="admin", csrf_token="csrf")
    assert client.post(url, data={"web_login_enabled": "0"}).status_code == 400
    client.post(url, data={"csrf_token": "csrf", "web_login_enabled": "invalid"})
    assert ns["customer_web_login_enabled"]("tank-a")


def test_existing_accounts_default_to_enabled_after_migration(portal):
    app, db, ns = portal
    db.execute("ALTER TABLE customer_accounts DROP COLUMN web_login_enabled")
    ns["ensure_customer_accounts_columns"](db)
    assert ns["customer_web_login_enabled"]("tank-a")
