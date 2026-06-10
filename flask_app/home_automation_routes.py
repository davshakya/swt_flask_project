import os
import threading
import time
from urllib.parse import urljoin

import requests
from flask import abort, jsonify, redirect, render_template, request, url_for


COMMAND_PAYLOAD_KEYS = {
    "switch": ("id", "state"),
    "fan": ("speed",),
    "all": ("state",),
}

HOME_AUTOMATION_STATUS = {}
HOME_AUTOMATION_LOCK = threading.Lock()
SWT_QUEUE_COMMAND_FN = None
HOME_AUTOMATION_VIEW_CONTEXT_FN = None
HOME_AUTOMATION_DEVICE_ACCESS_FN = None
HOME_AUTOMATION_MOBILE_USER_FN = None
HOME_AUTOMATION_MOBILE_SCOPE_FN = None


def set_home_automation_command_queue(queue_fn):
    global SWT_QUEUE_COMMAND_FN
    SWT_QUEUE_COMMAND_FN = queue_fn


def set_home_automation_view_context(context_fn):
    global HOME_AUTOMATION_VIEW_CONTEXT_FN
    HOME_AUTOMATION_VIEW_CONTEXT_FN = context_fn


def set_home_automation_device_access(access_fn):
    global HOME_AUTOMATION_DEVICE_ACCESS_FN
    HOME_AUTOMATION_DEVICE_ACCESS_FN = access_fn


def set_home_automation_mobile_access(user_fn, scope_fn):
    global HOME_AUTOMATION_MOBILE_USER_FN, HOME_AUTOMATION_MOBILE_SCOPE_FN
    HOME_AUTOMATION_MOBILE_USER_FN = user_fn
    HOME_AUTOMATION_MOBILE_SCOPE_FN = scope_fn


def list_home_automation_statuses():
    with HOME_AUTOMATION_LOCK:
        return {device_id: dict(status) for device_id, status in HOME_AUTOMATION_STATUS.items()}


def default_home_automation_context(default_device_id):
    return {
        "authenticated": True,
        "role": "public",
        "devices": [],
        "default_device_id": default_device_id,
        "can_select_devices": True,
    }


def record_home_automation_status(payload):
    if not isinstance(payload, dict):
        return
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id or payload.get("project") != "home_automation_switch_board":
        return
    snapshot = dict(payload)
    snapshot["cloud_last_seen"] = int(time.time())
    with HOME_AUTOMATION_LOCK:
        HOME_AUTOMATION_STATUS[device_id] = snapshot


def register_home_automation_routes(app):
    local_device_url = os.getenv("HA_LOCAL_DEVICE_URL", "http://192.168.1.50")
    cloud_base_url = os.getenv("HA_HOME_AUTOMATION_CLOUD_BASE_URL", "").rstrip("/")
    cloud_api_key = os.getenv("HA_HOME_AUTOMATION_CLOUD_API_KEY", "")
    default_device_id = os.getenv("HA_DEVICE_ID") or os.getenv("SWT_DEVICE_ID") or "sha_board_dev"
    request_timeout = float(os.getenv("HA_REQUEST_TIMEOUT", "6"))

    def local_url(path):
        return urljoin(local_device_url.rstrip("/") + "/", path.lstrip("/"))

    def cloud_headers():
        headers = {"Content-Type": "application/json"}
        if cloud_api_key:
            headers["Authorization"] = f"Bearer {cloud_api_key}"
        return headers

    def cloud_url(device_id, path):
        base = f"{effective_cloud_base_url()}/devices/{device_id}/"
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
        if not cloud_base_url and request.host:
            return None
        if not cloud_base_url:
            return jsonify({"error": "cloud_not_configured"}), 503
        return None

    def effective_cloud_base_url():
        return cloud_base_url or request.url_root.rstrip("/")

    def payload_for(command):
        if command not in COMMAND_PAYLOAD_KEYS:
            abort(404)
        payload = request.get_json(silent=True) or {}
        return {key: payload.get(key) for key in COMMAND_PAYLOAD_KEYS[command]}

    def validate_command_payload(command):
        payload = payload_for(command)
        if command == "switch":
            try:
                channel_id = int(payload.get("id"))
            except (TypeError, ValueError):
                return None, ("id must be a number", 400)
            state = str(payload.get("state") or "").lower()
            if state not in {"on", "off", "toggle"}:
                return None, ("state must be on, off, or toggle", 400)
            return {"id": channel_id, "state": state}, None

        if command == "fan":
            try:
                speed = int(payload.get("speed"))
            except (TypeError, ValueError):
                return None, ("speed must be a number", 400)
            if speed < 0 or speed > 5:
                return None, ("speed must be 0..5", 400)
            return {"speed": speed}, None

        if command == "all":
            state = str(payload.get("state") or "").lower()
            if state not in {"on", "off"}:
                return None, ("state must be on or off", 400)
            return {"state": state}, None

        abort(404)

    def home_command_string(command, payload):
        if command == "switch":
            return f"SWITCH:{payload['id']}:{payload['state'].upper()}"
        if command == "fan":
            return f"FAN:{payload['speed']}"
        if command == "all":
            return f"ALL:{payload['state'].upper()}"
        abort(404)

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

    def cloud_get(device_id, path, check_access=True):
        if check_access:
            access_response = require_device_access(device_id)
            if access_response:
                return access_response
        if not cloud_base_url:
            if path.strip("/") != "status":
                abort(404)
            with HOME_AUTOMATION_LOCK:
                latest = HOME_AUTOMATION_STATUS.get(device_id)
            if not latest:
                return jsonify({"error": "device_offline"}), 404
            body = dict(latest)
            return jsonify(body)

        response, exc = request_device("GET", cloud_url(device_id, path), headers=cloud_headers())
        return response if response else request_error("cloud", exc)

    def cloud_command(device_id, command, check_access=True):
        if check_access:
            access_response = require_device_access(device_id)
            if access_response:
                return access_response
        command_payload, error = validate_command_payload(command)
        if error:
            message, status_code = error
            return jsonify({"error": message}), status_code

        if not cloud_base_url:
            if SWT_QUEUE_COMMAND_FN is None:
                return jsonify({"error": "command_queue_unavailable"}), 503
            queued = SWT_QUEUE_COMMAND_FN(home_command_string(command, command_payload), target_device=device_id)
            status_code = queued[1] if isinstance(queued, tuple) else 200
            body = queued[0] if isinstance(queued, tuple) else queued
            return jsonify(body), status_code

        response, exc = request_device(
            "POST",
            cloud_url(device_id, command),
            json=command_payload,
            headers=cloud_headers(),
        )
        return response if response else request_error("cloud", exc)

    def home_context():
        if HOME_AUTOMATION_VIEW_CONTEXT_FN is None:
            return default_home_automation_context(default_device_id)
        context = HOME_AUTOMATION_VIEW_CONTEXT_FN() or {}
        context.setdefault("authenticated", True)
        context.setdefault("role", "public")
        context.setdefault("devices", [])
        context.setdefault("default_device_id", default_device_id)
        context.setdefault("can_select_devices", True)
        return context

    def require_device_access(device_id):
        if HOME_AUTOMATION_DEVICE_ACCESS_FN is None:
            return None
        response = HOME_AUTOMATION_DEVICE_ACCESS_FN(device_id)
        return response

    def require_mobile_device_id():
        if HOME_AUTOMATION_MOBILE_USER_FN is None or HOME_AUTOMATION_MOBILE_SCOPE_FN is None:
            return None, (jsonify({"error": "mobile_auth_unavailable"}), 503)
        user = HOME_AUTOMATION_MOBILE_USER_FN()
        if not user:
            return None, (jsonify({"error": "authentication required"}), 401)
        try:
            device_id = HOME_AUTOMATION_MOBILE_SCOPE_FN(request.values.get("device_id", type=str))
        except Exception:
            return None, (jsonify({"error": "forbidden"}), 403)
        if not device_id:
            return None, (jsonify({"error": "device_id_required"}), 400)
        return device_id, None

    @app.get("/home-automation")
    def home_automation():
        context = home_context()
        if not context.get("authenticated"):
            return redirect(context.get("login_url") or url_for("customer_login", next=request.path))
        return render_template(
            "home_automation.html",
            local_device_url=local_device_url,
            cloud_enabled=True,
            default_device_id=context.get("default_device_id") or default_device_id,
            registered_devices=context.get("devices") or [],
            viewer_role=context.get("role") or "public",
            can_select_devices=bool(context.get("can_select_devices", True)),
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

    @app.get("/api/mobile/home-automation/status")
    def api_mobile_home_automation_status():
        device_id, error_response = require_mobile_device_id()
        if error_response:
            return error_response
        return cloud_get(device_id, "/status", check_access=False)

    @app.post("/api/mobile/home-automation/<command>")
    def api_mobile_home_automation_command(command):
        device_id, error_response = require_mobile_device_id()
        if error_response:
            return error_response
        return cloud_command(device_id, command, check_access=False)
