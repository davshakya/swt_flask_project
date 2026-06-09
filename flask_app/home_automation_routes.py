import os
from urllib.parse import urljoin

import requests
from flask import abort, jsonify, render_template, request


COMMAND_PAYLOAD_KEYS = {
    "switch": ("id", "state"),
    "fan": ("speed",),
    "all": ("state",),
}


def register_home_automation_routes(app):
    local_device_url = os.getenv("HA_LOCAL_DEVICE_URL", "http://192.168.1.50")
    cloud_base_url = os.getenv("SALEWELL_CLOUD_BASE_URL", "").rstrip("/")
    cloud_api_key = os.getenv("SALEWELL_CLOUD_API_KEY", "")
    default_device_id = os.getenv("HA_DEVICE_ID", "sha_board_dev")
    request_timeout = float(os.getenv("HA_REQUEST_TIMEOUT", "6"))

    def local_url(path):
        return urljoin(local_device_url.rstrip("/") + "/", path.lstrip("/"))

    def cloud_headers():
        headers = {"Content-Type": "application/json"}
        if cloud_api_key:
            headers["Authorization"] = f"Bearer {cloud_api_key}"
        return headers

    def cloud_url(device_id, path):
        base = f"{cloud_base_url}/devices/{device_id}/"
        return urljoin(base, path.lstrip("/"))

    def proxy_response(response):
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text}
        return jsonify(body), response.status_code

    def request_device(method, url, **kwargs):
        try:
            response = requests.request(method, url, timeout=request_timeout, **kwargs)
            return proxy_response(response)
        except requests.RequestException as exc:
            return None, exc

    def request_error(target, exc):
        return jsonify({"error": f"{target}_unreachable", "detail": str(exc)}), 502

    def require_cloud():
        if not cloud_base_url:
            return jsonify({"error": "cloud_not_configured"}), 503
        return None

    def payload_for(command):
        if command not in COMMAND_PAYLOAD_KEYS:
            abort(404)
        payload = request.get_json(silent=True) or {}
        return {key: payload.get(key) for key in COMMAND_PAYLOAD_KEYS[command]}

    def local_get(path):
        response, exc = request_device("GET", local_url(path))
        return response if response else request_error("local_device", exc)

    def local_command(command):
        response, exc = request_device(
            "GET",
            local_url(command),
            params=payload_for(command),
        )
        return response if response else request_error("local_device", exc)

    def cloud_get(device_id, path):
        blocked = require_cloud()
        if blocked:
            return blocked
        response, exc = request_device("GET", cloud_url(device_id, path), headers=cloud_headers())
        return response if response else request_error("cloud", exc)

    def cloud_command(device_id, command):
        blocked = require_cloud()
        if blocked:
            return blocked
        response, exc = request_device(
            "POST",
            cloud_url(device_id, command),
            json=payload_for(command),
            headers=cloud_headers(),
        )
        return response if response else request_error("cloud", exc)

    @app.get("/home-automation")
    def home_automation():
        return render_template(
            "home_automation.html",
            local_device_url=local_device_url,
            cloud_enabled=bool(cloud_base_url),
            default_device_id=default_device_id,
        )

    @app.get("/api/home-automation/local/status")
    def api_home_automation_local_status():
        return local_get("/status")

    @app.post("/api/home-automation/local/<command>")
    def api_home_automation_local_command(command):
        return local_command(command)

    @app.get("/api/home-automation/cloud/<device_id>/status")
    def api_home_automation_cloud_status(device_id):
        return cloud_get(device_id, "/status")

    @app.post("/api/home-automation/cloud/<device_id>/<command>")
    def api_home_automation_cloud_command(device_id, command):
        return cloud_command(device_id, command)
