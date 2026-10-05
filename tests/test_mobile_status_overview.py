"""Exercise the status recovery route without starting database services."""
import ast
from pathlib import Path
from types import SimpleNamespace
from flask import Flask, jsonify, request

def route_namespace():
    source = Path(__file__).resolve().parents[1] / 'flask_app/server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'mobile_device_status')
    assert any(isinstance(d, ast.Name) and d.id == 'mobile_auth_required' for d in node.decorator_list)
    node.decorator_list = []
    calls = []
    snapshot = {'device_id': 'swt-test-000-000-008', 'motor': 'OFF', 'telemetry_status': 'stale'}
    config = {'cloud_feed_enabled': True}
    def scoped(requested):
        calls.append(('scope', requested))
        return snapshot['device_id']
    def forbidden(*args, **kwargs):
        raise AssertionError('overview must not run expensive or destructive bootstrap work')
    namespace = dict(request=request, jsonify=jsonify,
        mobile_customer_cloud_feed_block_response=lambda: None,
        current_mobile_scope_device_id=scoped,
        CAPACITY_FEATURES=SimpleNamespace(enabled=lambda _: False),
        load_dashboard_snapshot=lambda device, **kwargs: snapshot if device == snapshot['device_id'] else forbidden(),
        resolve_device_service_config=lambda *args, **kwargs: config,
        strip_ip_address_fields=lambda value, **kwargs: value,
        build_synchronized_status_payload=lambda value, **kwargs: {'pump': {'state': value['motor']}},
        resolve_mobile_user=lambda: {'role': 'customer', 'device_id': snapshot['device_id']},
        build_system_status_payload=forbidden, build_monitoring_summary_payload=forbidden,
        fetch_device_automation_settings=forbidden, build_current_saved_config=forbidden,
        pop_device_mobile_action=forbidden)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace, calls

def test_overview_preserves_stale_reading_and_authenticated_scope_without_side_effects():
    namespace, calls = route_namespace()
    app = Flask(__name__)
    with app.test_request_context('/api/mobile/device/status?overview=1&device_id=another-device'):
        payload = namespace['mobile_device_status']().get_json()
    assert calls == [('scope', 'another-device')]
    assert payload['snapshot']['device_id'] == payload['viewer']['device_id'] == 'swt-test-000-000-008'
    assert payload['snapshot']['telemetry_status'] == 'stale'
    assert payload['system_status']['synchronized_status']['pump']['state'] == 'OFF'

def test_overview_respects_cloud_feed_policy_before_reading_device():
    namespace, calls = route_namespace()
    namespace['mobile_customer_cloud_feed_block_response'] = lambda: ('disabled', 403)
    with Flask(__name__).test_request_context('/api/mobile/device/status?overview=1'):
        assert namespace['mobile_device_status']() == ('disabled', 403)
    assert calls == []
