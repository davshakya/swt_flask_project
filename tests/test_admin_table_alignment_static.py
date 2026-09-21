from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_admin_tables_are_centered_without_column_gaps():
    template = (ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    assert ".admin-table{border-spacing:0}" in template
    assert "text-align:center;vertical-align:middle;padding-left:2px;padding-right:2px" in template
    assert ".admin-table thead .sort-button{justify-content:center;text-align:center;white-space:nowrap}" in template
    assert ".admin-table th:nth-child(3) .sort-button{white-space:nowrap;overflow:visible}" in template


def test_active_path_uses_compact_node_names():
    server = (ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    assert 'else "M ↔ WIFI_LAN ↔ S"' in server
    assert 'else "M ↔ R1 ↔ R2 ↔ S"' in server
    assert "WIFI_LAN <-> Slave" not in server

def test_active_route_overrides_lan_standby_reachability():
    server = (ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    assert 'active_route = str(entry.get("direct_peer_route") or "").strip().upper()' in server
    assert "not active_route" in server
