from __future__ import annotations

import threading
import time
import urllib.request
import uuid

import pytest
from werkzeug.serving import make_server

import server as server_module
from server import app, get_db, upsert_customer_account
from tests.ui_test_support import build_status_payload, device_headers, ensure_known_admin_password


CHART_JS_STUB = """
window.Chart = function(_ctx, config) {
  this.data = (config && config.data) || { labels: [], datasets: [{ data: [] }] };
  this.options = (config && config.options) || {};
};
window.Chart.defaults = {};
window.Chart.prototype.update = function() {};
window.Chart.prototype.destroy = function() {};
"""


def cleanup_device_records(device_id: str) -> None:
    normalized_device_id = str(device_id or "").strip()
    if not normalized_device_id:
        return
    with get_db() as db:
        db.execute("DELETE FROM ignored_devices WHERE device_id = ?", (normalized_device_id,))
        db.execute("DELETE FROM customer_accounts WHERE device_id = ?", (normalized_device_id,))
        db.execute("DELETE FROM registered_devices WHERE device_id = ?", (normalized_device_id,))
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (normalized_device_id,))
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (normalized_device_id,))
        db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (normalized_device_id,))
        db.execute("DELETE FROM ops_audit_log WHERE device_id = ?", (normalized_device_id,))
    server_module.forget_registered_device_touch(normalized_device_id)
    server_module.clear_runtime_caches(normalized_device_id)


@pytest.fixture
def known_admin_password():
    return ensure_known_admin_password()


@pytest.fixture
def customer_test_account(monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    device_id = f"swt-playwright-{suffix}"
    device_key = f"PlaywrightKey{suffix}!"
    password = f"CustomerPass{suffix}!"
    display_name = "Playwright Customer"

    cleanup_device_records(device_id)
    monkeypatch.setitem(server_module.DEVICE_KEY_MAP, device_id, device_key)
    upsert_customer_account(device_id, password, display_name=display_name)

    response = app.test_client().post(
        "/status",
        json=build_status_payload(
            level=64.0,
            ai_usage_rate=0.42,
            tank_health=94.0,
            firmware_version="1.2.0-test",
        ),
        headers=device_headers(device_id=device_id, device_key=device_key),
    )
    assert response.status_code == 200

    yield {
        "device_id": device_id,
        "device_key": device_key,
        "password": password,
        "display_name": display_name,
    }

    cleanup_device_records(device_id)


@pytest.fixture(scope="session")
def live_server_url():
    server = make_server("127.0.0.1", 0, app, threaded=True)
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    deadline = time.time() + 10
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception as exc:  # pragma: no cover - best effort startup polling
            last_error = exc
            time.sleep(0.1)
    else:  # pragma: no cover - would only happen on broken app startup
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        raise RuntimeError(f"Timed out starting live Flask test server: {last_error}")

    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture(scope="session")
def playwright_browser():
    playwright_sync = pytest.importorskip(
        "playwright.sync_api",
        reason="Install Playwright to run browser UI tests.",
    )
    with playwright_sync.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except playwright_sync.Error:
            pytest.skip(
                "Playwright Chromium is not installed. Run `python -m playwright install chromium`.",
            )
        yield browser
        browser.close()


@pytest.fixture
def browser_context(playwright_browser):
    context = playwright_browser.new_context(viewport={"width": 1440, "height": 1080})
    context.route(
        "**/chart.js*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/javascript",
            body=CHART_JS_STUB,
        ),
    )
    yield context
    context.close()


@pytest.fixture
def page(browser_context):
    page = browser_context.new_page()
    page.set_default_timeout(10000)
    page.set_default_navigation_timeout(10000)
    yield page
    page.close()
