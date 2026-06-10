import os
from pathlib import Path

from flask import Flask, redirect

from flask_app.home_automation_routes import (
    record_home_automation_status,
    register_home_automation_routes,
    set_home_automation_view_context,
)


PROJECT_ROOT = Path(__file__).resolve().parent
APP_ROOT = PROJECT_ROOT / "flask_app"


app = Flask(
    __name__,
    template_folder=str(APP_ROOT / "templates"),
    static_folder=str(APP_ROOT / "static"),
)


DEMO_DEVICES = [
    {
        "device_id": "sha_000-000-000-001",
        "label": "Devendra",
        "email": "davshakya@gmail.com",
        "ip": "192.168.1.2",
        "rssi": -54,
        "fan_speed": 2,
        "channels": [
            {"id": 1, "name": "Living Room Light", "state": True},
            {"id": 2, "name": "Bedroom Light", "state": False},
            {"id": 3, "name": "Living Room Fan", "state": True},
        ],
    },
    {
        "device_id": "sha_000-000-000-002",
        "label": "Ranjana",
        "email": "kmranjanaverma@gmail.com",
        "ip": "192.168.1.11",
        "rssi": -59,
        "fan_speed": 0,
        "channels": [
            {"id": 1, "name": "Kitchen Light", "state": False},
            {"id": 2, "name": "Balcony Light", "state": False},
            {"id": 3, "name": "Hall Fan", "state": False},
        ],
    },
    {
        "device_id": "sha_000-000-000-003",
        "label": "Manav",
        "email": "manav.shakya2015@gmail.com",
        "ip": "192.168.1.14",
        "rssi": -53,
        "fan_speed": 4,
        "channels": [
            {"id": 1, "name": "Shop Sign", "state": True},
            {"id": 2, "name": "Counter Light", "state": True},
            {"id": 3, "name": "Exhaust Fan", "state": True},
        ],
    },
    {
        "device_id": "sha_000-000-000-005",
        "label": "demo",
        "email": "demo@gmail.com",
        "ip": "192.168.1.3",
        "rssi": -58,
        "fan_speed": 1,
        "channels": [
            {"id": 1, "name": "Porch Light", "state": False},
            {"id": 2, "name": "Garden Light", "state": True},
            {"id": 3, "name": "Room Fan", "state": False},
        ],
    },
]


def seed_demo_statuses():
    for device in DEMO_DEVICES:
        record_home_automation_status(
            {
                "project": "home_automation_switch_board",
                "device_id": device["device_id"],
                "ip": device["ip"],
                "rssi": device["rssi"],
                "fan_speed": device["fan_speed"],
                "channels": device["channels"],
            }
        )


def demo_view_context():
    devices = []
    for device in DEMO_DEVICES:
        devices.append(
            {
                "device_id": device["device_id"],
                "label": device["label"],
                "email": device["email"],
                "access_label": "Admin/customer",
                "credentials_label": "Credentials active",
                "has_credentials": True,
                "online": True,
                "ip": device["ip"],
                "rssi": device["rssi"],
                "fan_speed": device["fan_speed"],
                "channels": device["channels"],
                "channel_count": len(device["channels"]),
                "can_delete": False,
            }
        )
    return {
        "authenticated": True,
        "role": "admin",
        "devices": devices,
        "default_device_id": devices[0]["device_id"],
        "can_select_devices": True,
        "admin_dashboard_url": "#boards",
    }


seed_demo_statuses()
set_home_automation_view_context(demo_view_context)
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
