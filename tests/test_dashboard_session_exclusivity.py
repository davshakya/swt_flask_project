from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"


def test_dashboard_login_get_uses_existing_matching_role_session():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def handle_role_login(mode):")
    function_source = source[function_start : source.index("\ndef render_dashboard_password_page", function_start)]

    assert 'expected_role = "admin" if mode == "admin" else "customer"' in function_source
    assert 'if request.method == "GET" and activate_dashboard_identity(expected_role):' in function_source
    assert "return redirect(dashboard_home_url(expected_role))" in function_source


def test_dashboard_login_success_stores_role_specific_browser_identity():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def handle_role_login(mode):")
    function_source = source[function_start : source.index("\ndef render_dashboard_password_page", function_start)]

    login_success = function_source[
        function_source.index('if authenticated_user and authenticated_user["role"] == expected_role:')
        : function_source.index('logger.info(', function_source.index('if authenticated_user and authenticated_user["role"] == expected_role:'))
    ]
    assert "store_dashboard_identity(authenticated_user)" in login_success
    assert "session.clear()" not in login_success


def test_dashboard_session_stores_admin_and_customer_identities_separately():
    source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "def store_dashboard_identity(authenticated_user):" in source
    assert 'session[f"{prefix}_logged_in"] = True' in source
    assert 'session[f"{prefix}_username"] = authenticated_user["username"]' in source
    assert 'session[f"{prefix}_device_id"] = authenticated_user.get("device_id")' in source
    assert "def activate_dashboard_identity(role):" in source
    assert 'session.get(f"{prefix}_auth_marker")' in source


def test_dashboard_wrong_role_refresh_redirects_to_active_dashboard():
    source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "def role_mismatch_response():" in source
    assert 'if request.method in {"GET", "HEAD"}:' in source
    assert "return redirect(dashboard_home_url())" in source
    assert "return role_mismatch_response()" in source


def test_public_homepage_does_not_redirect_logged_in_users_to_dashboard():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index('def dashboard():')
    function_source = source[function_start : source.index('\n\n@app.route("/homepage")', function_start)]

    assert "return render_login_page(" in function_source
    assert "is_logged_in()" not in function_source
    assert 'redirect(url_for("admin_customers"))' not in function_source
    assert "render_dashboard_page" not in function_source
