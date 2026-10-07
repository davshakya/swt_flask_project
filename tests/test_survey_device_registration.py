from pathlib import Path

from flask_app import server


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAIL_TEMPLATE = PROJECT_ROOT / "flask_app" / "templates" / "admin_survey_response_detail.html"
LIST_TEMPLATE = PROJECT_ROOT / "flask_app" / "templates" / "admin_survey_responses.html"


def test_survey_setup_defaults_use_available_site_data():
    response = {
        "answers_json": """{
            "water_source":"Multiple sources",
            "tank1_capacity":"1500",
            "tank2_details":"Secondary overhead tank",
            "customer_requirements":"Automatic pump ON/OFF"
        }"""
    }

    setup = server.survey_device_setup_defaults(response)

    assert setup["device_setup_type"] == "hybrid"
    assert setup["municipal_sensor_enabled"] is True
    assert setup["source_tank_monitoring_enabled"] is True
    assert setup["municipal_valve_enabled"] is True
    assert setup["slave_device_enabled"] is True
    assert setup["auto_mode_enabled"] is False
    assert setup["upper_tank_capacity_liters"] == 1500


def test_survey_registration_route_has_server_side_acceptance_gate():
    source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    route_start = source.index("def admin_survey_register_device(response_id):")
    route_end = source.index("def admin_survey_delete_device(response_id):", route_start)
    route = source[route_start:route_end]

    assert '!= "accepted"' in route
    assert "Accept this survey before registering a device." in route
    assert "register_device_credentials(" in route
    assert "upsert_device_service_config(" in route
    assert '"auto_mode_enabled": False' in route
    assert 'form_flag("auto_mode_enabled"' not in route


def test_explicit_device_registration_restores_deleted_device_telemetry():
    source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    start = source.index("def register_device_credentials(")
    body = source[start : source.index("def list_registered_device_ids", start)]

    assert "forget_ignored_device(normalized_device_id)" in body
    assert body.index("forget_ignored_device(normalized_device_id)") < body.index("remember_registered_device(")


def test_admin_can_accept_reject_and_delete_survey_with_csrf_forms():
    detail = DETAIL_TEMPLATE.read_text(encoding="utf-8")
    listing = LIST_TEMPLATE.read_text(encoding="utf-8")

    assert "admin_survey_review" in detail
    assert 'name="decision" value="accepted"' in detail
    assert 'name="decision" value="rejected"' in detail
    assert "admin_survey_delete" in detail
    assert "csrf_token" in detail
    assert "response.review_status == 'accepted'" in detail
    assert "Accept a survey before configuring and registering its device." in listing


def test_survey_schema_tracks_review_and_registered_device():
    source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '"review_status": "VARCHAR(20) NOT NULL DEFAULT \'pending\'"' in source
    assert '"registered_device_id": "VARCHAR(255)"' in source
    assert "ensure_survey_responses_columns(cursor)" in source
