from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_authenticated_admin_and_mobile_node_replacement_controls_exist():
    source = (ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    template = (ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert '@app.route("/devices/<device_id>/replace-node", methods=["POST"])' in source
    assert "@admin_required" in source
    assert "@csrf_protect" in source
    assert '@app.route("/api/mobile/device/replace-node", methods=["POST"])' in source
    assert "@mobile_auth_required" in source
    assert 'command = f"replace_node:{role}"' in source
    assert 'value="slave"' in template
    assert 'value="repeater"' in template
    assert "Power off the old node first" in template
