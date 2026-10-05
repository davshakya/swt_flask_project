from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent


def test_firmware_reports_slave_side_peer_link_quality_to_cloud():
    source = (WORKSPACE_ROOT / "swt_firmware_project" / "src" / "two_node_udp.cpp").read_text(encoding="utf-8")

    assert 'doc["direct_peer_received_packets"]' in source
    assert 'doc["direct_peer_link_quality_pct"]' in source
    assert "peerLinkMetricsVersion = 1U" in source
    assert "lastSlavePeerLinkQualityPct" in source


def test_flask_persists_and_displays_peer_link_quality():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    template_source = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert '"direct_peer_received_packets": "BIGINT"' in server_source
    assert '"direct_peer_link_quality_pct": "INTEGER"' in server_source
    assert '("Peer Link Signal"' in server_source
    assert '("Peer Link Quality"' in server_source
    assert 'label:"Peer Link Signal"' in template_source
    assert 'label:"Peer Link Quality"' in template_source
