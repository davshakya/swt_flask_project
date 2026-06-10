from pathlib import Path

from flask import Flask

from flask_app.home_automation_routes import (
    HOME_AUTOMATION_STATUS,
    record_home_automation_status,
    register_home_automation_routes,
    set_home_automation_command_queue,
    set_home_automation_device_access,
    set_home_automation_mobile_access,
    set_home_automation_view_context,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_app(monkeypatch, device_key=""):
    HOME_AUTOMATION_STATUS.clear()
    set_home_automation_command_queue(None)
    set_home_automation_device_access(None)
    set_home_automation_mobile_access(None, None)
    set_home_automation_view_context(None)
    monkeypatch.delenv("SALEWELL_CLOUD_BASE_URL", raising=False)
    monkeypatch.setenv("HA_DEVICE_ID", "sha_000-000-000-001")
    if device_key:
        monkeypatch.setenv("HA_DEVICE_KEY", device_key)
    else:
        monkeypatch.delenv("HA_DEVICE_KEY", raising=False)
    app = Flask(__name__)
    register_home_automation_routes(app)
    return app


def test_cloud_command_uses_swt_device_command_queue(monkeypatch):
    queued_commands = []
    client = make_app(monkeypatch).test_client()
    set_home_automation_command_queue(
        lambda command, target_device=None: queued_commands.append((command, target_device))
        or {"status": "queued", "command": command, "target_device": target_device}
    )

    offline = client.get("/api/home-automation/cloud/sha_000-000-000-001/status")
    assert offline.status_code == 404
    assert offline.json["error"] == "device_offline"

    queued = client.post(
        "/api/home-automation/cloud/sha_000-000-000-001/switch",
        json={"id": 1, "state": "on"},
    )
    assert queued.status_code == 200
    assert queued.json["status"] == "queued"
    assert queued.json["command"] == "SWITCH:1:ON"
    assert queued_commands == [("SWITCH:1:ON", "sha_000-000-000-001")]

    record_home_automation_status(
        {
            "project": "home_automation_switch_board",
            "device_id": "sha_000-000-000-001",
            "channels": [],
            "fan_speed": 0,
            "ip": "192.168.1.50",
        }
    )

    online = client.get("/api/home-automation/cloud/sha_000-000-000-001/status")
    assert online.status_code == 200
    assert online.json["device_id"] == "sha_000-000-000-001"
    assert online.json["ip"] == "192.168.1.50"


def test_cloud_command_reports_unavailable_when_swt_queue_is_not_bound(monkeypatch):
    client = make_app(monkeypatch).test_client()

    response = client.post(
        "/api/home-automation/cloud/sha_000-000-000-001/fan",
        json={"speed": 3},
    )
    assert response.status_code == 503
    assert response.json["error"] == "command_queue_unavailable"


def test_cloud_status_requires_registered_device_access(monkeypatch):
    client = make_app(monkeypatch).test_client()
    set_home_automation_device_access(lambda device_id: ({"error": "forbidden"}, 403))

    response = client.get("/api/home-automation/cloud/other_board/status")

    assert response.status_code == 403
    assert response.json["error"] == "forbidden"


def test_mobile_home_automation_routes_use_authenticated_customer_scope(monkeypatch):
    queued_commands = []
    client = make_app(monkeypatch).test_client()
    set_home_automation_mobile_access(
        lambda: {"role": "customer", "device_id": "sha_000-000-000-001"},
        lambda requested_device_id=None: requested_device_id or "sha_000-000-000-001",
    )
    set_home_automation_command_queue(
        lambda command, target_device=None: queued_commands.append((command, target_device))
        or {"status": "queued", "command": command, "target_device": target_device}
    )
    record_home_automation_status(
        {
            "project": "home_automation_switch_board",
            "device_id": "sha_000-000-000-001",
            "channels": [{"id": 1, "name": "Light", "state": True}],
            "fan_speed": 2,
            "ip": "192.168.1.50",
        }
    )

    status = client.get("/api/mobile/home-automation/status")
    assert status.status_code == 200
    assert status.json["device_id"] == "sha_000-000-000-001"
    assert status.json["channels"][0]["name"] == "Light"

    command = client.post("/api/mobile/home-automation/all", json={"state": "off"})
    assert command.status_code == 200
    assert command.json["command"] == "ALL:OFF"
    assert queued_commands == [("ALL:OFF", "sha_000-000-000-001")]


def test_mobile_home_automation_requires_mobile_auth(monkeypatch):
    client = make_app(monkeypatch).test_client()
    set_home_automation_mobile_access(lambda: None, lambda requested_device_id=None: requested_device_id)

    response = client.get("/api/mobile/home-automation/status")

    assert response.status_code == 401
    assert response.json["error"] == "authentication required"


def test_home_automation_admin_registration_route_exists():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    template_source = (PROJECT_ROOT / "flask_app" / "templates" / "home_automation.html").read_text(encoding="utf-8")
    admin_source = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert '@app.route("/admin/home-automation/register", methods=["POST"])' in server_source
    assert "def admin_home_automation_register_device():" in server_source
    assert "Home Automation device IDs must start with sha_." in server_source
    assert "admin_home_automation_register_device" in template_source
    assert 'href="{{ url_for(\'admin_customers\') }}">Back to Admin Dashboard</a>' in template_source
    assert 'pattern="sha_.*"' in template_source
    assert "Home Automation" in admin_source
