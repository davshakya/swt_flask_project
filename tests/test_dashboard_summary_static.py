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

    # Android keeps the expensive sections materialized, but its primary tank
    # and motor snapshot must match the live Flask device table.
    assert "latest_snapshot = load_dashboard_snapshot(scoped_device_id)" in mobile_body
    assert 'summary["snapshot"] = strip_ip_address_fields(' in mobile_body


def test_dashboard_displays_persisted_summary_last_updated_time():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "payload.last_updated" in template
    assert '"Last updated"' in template


def test_device_monitoring_summary_reuses_snapshot_instead_of_scanning_inventory():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def build_monitoring_summary_payload(")
    function_body = source[function_start : source.index("\n\ndef build_ops_dashboard_payload", function_start)]

    assert "[build_admin_device_entry(normalized_device_id, snapshot=snapshot)]" in function_body
    assert "else fetch_device_inventory(limit=20)" in function_body
    assert "device_ids=[normalized_device_id]" not in function_body


def test_dashboard_summary_refresh_serializes_and_retries_connection_resets():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def refresh_dashboard_summary(device_id):")
    function_end = source.index("\n\ndef schedule_dashboard_summary_refresh", function_start)
    function_body = source[function_start:function_end]

    assert "dashboard_summary_worker_lock = threading.Lock()" in source
    assert "with dashboard_summary_worker_lock:" in function_body
    assert "run_with_database_lock_retries(" in function_body
    assert "attempts=3" in function_body
    assert "retry_connection_errors=True" in function_body
