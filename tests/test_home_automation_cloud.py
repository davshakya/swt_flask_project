from flask import Flask

from flask_app.home_automation_routes import (
    HOME_AUTOMATION_STATUS,
    record_home_automation_status,
    register_home_automation_routes,
    set_home_automation_command_queue,
)


def make_app(monkeypatch, device_key=""):
    HOME_AUTOMATION_STATUS.clear()
    set_home_automation_command_queue(None)
    monkeypatch.delenv("SALEWELL_CLOUD_BASE_URL", raising=False)
    monkeypatch.setenv("HA_DEVICE_ID", "sha_board_dev")
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

    offline = client.get("/api/home-automation/cloud/sha_board_dev/status")
    assert offline.status_code == 404
    assert offline.json["error"] == "device_offline"

    queued = client.post(
        "/api/home-automation/cloud/sha_board_dev/switch",
        json={"id": 1, "state": "on"},
    )
    assert queued.status_code == 200
    assert queued.json["status"] == "queued"
    assert queued.json["command"] == "SWITCH:1:ON"
    assert queued_commands == [("SWITCH:1:ON", "sha_board_dev")]

    record_home_automation_status(
        {
            "project": "home_automation_switch_board",
            "device_id": "sha_board_dev",
            "channels": [],
            "fan_speed": 0,
            "ip": "192.168.1.50",
        }
    )

    online = client.get("/api/home-automation/cloud/sha_board_dev/status")
    assert online.status_code == 200
    assert online.json["device_id"] == "sha_board_dev"
    assert online.json["ip"] == "192.168.1.50"


def test_cloud_command_reports_unavailable_when_swt_queue_is_not_bound(monkeypatch):
    client = make_app(monkeypatch).test_client()

    response = client.post(
        "/api/home-automation/cloud/sha_board_dev/fan",
        json={"speed": 3},
    )
    assert response.status_code == 503
    assert response.json["error"] == "command_queue_unavailable"
