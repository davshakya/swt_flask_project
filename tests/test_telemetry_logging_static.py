from pathlib import Path


SERVER_SOURCE = (Path(__file__).resolve().parents[1] / "flask_app" / "server.py").read_text(
    encoding="utf-8"
)


def test_successful_high_frequency_telemetry_is_not_logged_at_info():
    marker = '"Saved tank level via %s: %s | Motor: %s | Mode: %s | Device: %s"'
    marker_offset = SERVER_SOURCE.index(marker)
    call_prefix = SERVER_SOURCE[max(0, marker_offset - 80) : marker_offset]

    assert "logger.debug(" in call_prefix
    assert "logger.info(" not in call_prefix
