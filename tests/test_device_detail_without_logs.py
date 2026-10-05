from pathlib import Path
import re
import shutil
import subprocess

from jinja2 import Environment

TEMPLATE = Path(__file__).resolve().parents[1] / "flask_app/templates/device_detail.html"


def test_device_detail_has_no_activity_log_ui_or_polling():
    text = TEMPLATE.read_text(encoding="utf-8")
    Environment().parse(text)
    for removed in ("device-logs", "refreshActivityEvents", "ACTIVITY_REFRESH_MS", "latestEvents",
                    'new URL("/events"', "activityRefreshTimer", "Open Logs"):
        assert removed not in text
    assert 'statusUrl.searchParams.set("events","0")' in text
    assert "deviceRefreshTimer=setInterval" in text
    assert "name=\"web_login_enabled\"" in text


def test_remaining_device_detail_scripts_parse():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("Node is unavailable")
    text = TEMPLATE.read_text(encoding="utf-8")
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S):
        # Server-rendered values are expressions; substitute neutral values for syntax checking.
        script = re.sub(r"{{.*?}}", "null", script, flags=re.S)
        script = re.sub(r"{%.*?%}", "", script, flags=re.S)
        subprocess.run([node, "--check"], input=script, text=True, encoding="utf-8", capture_output=True, check=True)
