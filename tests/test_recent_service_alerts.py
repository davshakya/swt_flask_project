import ast
import sqlite3
from pathlib import Path


def test_recent_alert_history_includes_resolved_scopes_device_and_uses_latest_issue():
    source = (Path(__file__).resolve().parents[1] / "flask_app" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "fetch_recent_service_alerts")
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE ops_alerts (id INTEGER, device_id TEXT, kind TEXT, severity TEXT, message TEXT, created_at TEXT, updated_at TEXT, active INTEGER)")
    rows = [
        (1, "tank", "leak", "danger", "old leak", "01", "01", 1),
        (2, "tank", "sensor", "warning", "sensor fixed", "02", "05", 0),
        (3, "tank", "leak", "danger", "leak recurred", "03", "06", 1),
        (4, "tank", "pump", "danger", "pump fixed", "04", "04", 0),
        (5, "other", "leak", "danger", "private", "09", "09", 1),
        (6, "tank", "power", "warning", "older", "00", "00", 0),
        (7, "tank", "leak", "danger", "retired duplicate", "03", "08", 0),
    ]
    db.executemany("INSERT INTO ops_alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    namespace = {"get_db": lambda: db, "normalize_device_id": lambda value: value}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "server.py", "exec"), namespace)
    fetch = namespace["fetch_recent_service_alerts"]
    result = fetch("tank")
    assert [row["id"] for row in result] == [3, 2, 4]
    assert [row["active"] for row in result] == [1, 0, 0]
    assert fetch(None) == []
    assert fetch("unknown") == []
    assert '"recent_alerts": fetch_recent_service_alerts(device_id=device_id)' in source
    db.close()
