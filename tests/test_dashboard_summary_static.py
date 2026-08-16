from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"
TEMPLATE = PROJECT_ROOT / "flask_app" / "templates" / "index.html"


def test_dashboard_summary_is_persisted_and_reconciled():
    source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS dashboard_summaries" in source
    assert "schedule_dashboard_summary_refresh(cleaned.get(\"device_id\"))" in source
    assert "schedule_dashboard_summary_refresh(affected_device_id)" in source
    assert "DASHBOARD_SUMMARY_RECONCILE_SECONDS" in source
    assert "DASHBOARD_SUMMARY_MIN_REFRESH_SECONDS" in source
    assert "DASHBOARD_SUMMARY_RECONCILE_BATCH_SIZE" in source
    assert "MAX(dashboard_summaries.updated_at) < ?" in source
    assert "dashboard_summary_reconciler_stop.wait(DASHBOARD_SUMMARY_RECONCILE_SECONDS)" in source
    assert "start_dashboard_summary_reconciler()" in source
    assert '"DASHBOARD_SUMMARY_RECONCILIATION_ENABLED", default=False' in source


def test_analytics_requests_do_not_write_device_events_by_default():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    build_start = source.index("def build_analytics(")
    build_body = source[build_start : source.index("\n\ndef build_cached_fixed_ai_analytics", build_start)]

    assert 'ANALYTICS_SYNC_EVENTS_ON_REQUEST = env_flag("ANALYTICS_SYNC_EVENTS_ON_REQUEST", default=False)' in source
    assert "sync_device_events(device_id=normalized_device_id)" not in build_body


def test_customer_and_mobile_bootstrap_only_read_materialized_summary():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    web_start = source.index("def dashboard_bootstrap():")
    web_body = source[web_start : source.index('\n\n@app.route("/dashboard/local-sync"', web_start)]
    mobile_start = source.index("def mobile_bootstrap():")
    mobile_body = source[mobile_start : source.index('\n\n@app.route("/api/mobile/analytics")', mobile_start)]

    for route_body in (web_body, mobile_body):
        assert "load_persisted_dashboard_summary(scoped_device_id)" in route_body
        assert "build_monitoring_summary_payload(" not in route_body
        assert "build_events(" not in route_body
        assert "fetch_audit_events(" not in route_body


def test_dashboard_displays_persisted_summary_last_updated_time():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "payload.last_updated" in template
    assert '"Last updated"' in template
