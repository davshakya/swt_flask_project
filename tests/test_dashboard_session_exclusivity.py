from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"


def test_dashboard_login_get_switch_clears_other_role_session():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def handle_role_login(mode):")
    function_source = source[function_start : source.index("\ndef render_dashboard_password_page", function_start)]

    assert 'expected_role = "admin" if mode == "admin" else "customer"' in function_source
    assert 'if request.method == "GET" and is_logged_in():' in function_source
    assert "if current_user_role() == expected_role:" in function_source
    assert "session.clear()" in function_source


def test_dashboard_login_success_replaces_existing_browser_identity():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def handle_role_login(mode):")
    function_source = source[function_start : source.index("\ndef render_dashboard_password_page", function_start)]

    login_success = function_source[
        function_source.index('if authenticated_user and authenticated_user["role"] == expected_role:')
        : function_source.index('logger.info(', function_source.index('if authenticated_user and authenticated_user["role"] == expected_role:'))
    ]
    assert "session.clear()" in login_success
    assert login_success.index("session.clear()") < login_success.index('session["logged_in"] = True')
    assert 'session["role"] = authenticated_user["role"]' in login_success
    assert 'session["device_id"] = authenticated_user.get("device_id")' in login_success
