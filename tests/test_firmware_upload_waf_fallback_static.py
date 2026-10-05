from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
TEMPLATE_SOURCE = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")


def test_authenticated_json_firmware_upload_fallback_is_available():
    assert '@app.route("/admin/customers/<device_id>/artifact-intake", methods=["POST"])' in SERVER_SOURCE
    route_body = SERVER_SOURCE.split("def admin_device_artifact_intake", 1)[0]
    assert route_body.rstrip().endswith("@csrf_protect")
    assert "base64.b64decode(encoded_payload, validate=True)" in SERVER_SOURCE
    assert "create_firmware_artifact(" in SERVER_SOURCE


def test_browser_retries_litespeed_403_without_multipart_form_data():
    assert "if(request.status===0||request.status===403){sendJsonFallback();return;}" in TEMPLATE_SOURCE
    assert 'request.addEventListener("error",()=>{' in TEMPLATE_SOURCE
    assert 'request.addEventListener("timeout",()=>{sendJsonFallback();});' in TEMPLATE_SOURCE
    assert 'request.addEventListener("abort",()=>{sendJsonFallback();});' in TEMPLATE_SOURCE
    assert 'form.action.replace(/\\/firmware$/,"/artifact-intake")' in TEMPLATE_SOURCE
    assert '"Content-Type":"application/json"' in TEMPLATE_SOURCE
