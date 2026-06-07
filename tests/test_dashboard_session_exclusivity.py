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


def test_dashboard_login_registers_one_active_browser_session_per_user():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def store_dashboard_identity(authenticated_user):")
    function_source = source[function_start : source.index("\ndef activate_dashboard_identity", function_start)]

    assert "register_active_platform_session(" in function_source
    assert "SESSION_PLATFORM_DASHBOARD" in function_source
    assert 'session[f"{prefix}_platform_session_id"] = platform_session_id' in function_source


def test_dashboard_session_validation_rejects_replaced_browser_session():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def is_logged_in():")
    function_source = source[function_start : source.index("\ndef login_required", function_start)]

    assert "active_platform_session_matches(" in function_source
    assert "SESSION_PLATFORM_DASHBOARD" in function_source
    assert "clear_dashboard_identity()" in function_source


def test_mobile_tokens_register_and_validate_one_active_android_session_per_user():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    issue_start = source.index("def issue_mobile_token(user):")
    issue_source = source[issue_start : source.index("\ndef resolve_mobile_user", issue_start)]
    resolve_start = source.index("def resolve_mobile_user():")
    resolve_source = source[resolve_start : source.index("\ndef mobile_auth_required", resolve_start)]

    assert "register_active_platform_session(" in issue_source
    assert "SESSION_PLATFORM_ANDROID" in issue_source
    assert '"platform_session_id": platform_session_id' in issue_source
    assert "active_platform_session_matches(" in resolve_source
    assert "SESSION_PLATFORM_ANDROID" in resolve_source
    assert 'g.mobile_auth_error = "session_replaced"' in resolve_source


def test_android_sessions_use_configurable_sso_limit():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    register_start = source.index("def register_active_platform_session(")
    register_source = source[register_start : source.index("\ndef active_platform_session_matches", register_start)]
    validate_start = source.index("def active_platform_session_matches(")
    validate_source = source[validate_start : source.index("\ndef clear_active_platform_session", validate_start)]

    assert "def normalize_android_sso_session_limit(" in source
    assert "def active_platform_session_limit(" in source
    assert "android_sso_session_limit" in source
    assert "serialize_active_platform_sessions(active_sessions)" in register_source
    assert "active_sessions[-session_limit:]" not in register_source
    assert "active_platform_sessions(platform, role, username=username, device_id=device_id)" in validate_source


def test_mobile_login_preserves_existing_android_sessions():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def register_active_platform_session(")
    function_source = source[function_start : source.index("\ndef active_platform_session_matches", function_start)]

    assert "android_sso_login_blocked(" not in function_source
    assert '"code": "android_session_limit_reached"' not in function_source
    assert "active_sessions.append(next_session_id)" in function_source
    assert "active_platform_session_limit(" not in function_source


def test_mobile_logout_clears_the_current_android_session():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = source.index('@app.route("/api/mobile/auth/logout", methods=["POST"])')
    function_source = source[route_start : source.index("\n\n@app.route(\"/api/mobile/bootstrap\")", route_start)]

    assert '@app.route("/api/mobile/auth/logout", methods=["POST"])' in function_source
    assert "@mobile_auth_required" in function_source
    assert "clear_active_platform_session(" in function_source
    assert "SESSION_PLATFORM_ANDROID" in function_source
    assert 'session_id=user.get("platform_session_id")' in function_source


def test_admin_runtime_configuration_save_preserves_android_sessions():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = source.index('@app.route("/devices/<device_id>/configuration", methods=["POST"])')
    function_source = source[route_start : source.index('\n\n@app.route("/devices/<device_id>/mobile/logout"', route_start)]

    assert "clear_active_platform_session(" not in function_source
    assert "SESSION_PLATFORM_ANDROID" not in function_source
    assert '"android_sessions_preserved": True' in function_source
    assert "Android app sessions were signed out" not in function_source


def test_android_session_validation_does_not_trim_existing_sessions():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def active_platform_sessions(")
    function_source = source[function_start : source.index("\ndef active_platform_session_count", function_start)]

    assert "parse_active_platform_sessions(get_app_setting(setting_key, \"\"))" in function_source
    assert "active_platform_session_limit(" not in function_source
    assert "set_app_setting(" not in function_source


def test_replaced_mobile_session_returns_distinct_response_without_auto_refresh():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def mobile_auth_required(view):")
    function_source = source[function_start : source.index("\ndef current_mobile_scope_device_id", function_start)]

    assert 'getattr(g, "mobile_auth_error", None) == "session_replaced"' in function_source
    assert '"code": "session_replaced"' in function_source
    assert "), 409" in function_source


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
