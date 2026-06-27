from flask_app import server


def test_build_events_falls_back_to_current_status_when_device_has_no_activity():
    events = server.build_events(limit=5, device_id="swt-999-999-999-999")

    assert events
    assert events[0]["kind"] == "device_config_current_status"
    assert "No historical activity yet" in events[0]["message"]
    assert events[0]["details"]["device_id"] == "swt-999-999-999-999"
