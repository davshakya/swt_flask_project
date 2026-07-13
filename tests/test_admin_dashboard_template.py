from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_admin_customer_page_renders_one_popup_status_message_slot():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert template.count('id="upload_result_panel"') == 1
    assert template.count("data-upload-result-message") == 2
    assert "data-auto-open-panel" not in template


def test_landing_page_uses_compressed_responsive_marketing_images():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'rel="preload" as="image" type="image/webp"' in template
    assert "smart-water-tank-hero-ai-1280.webp" in template
    assert template.count("<source type=\"image/webp\"") >= 7
    assert template.count('decoding="async"') >= 7
    assert template.count('width="1536" height="1024"') >= 7


def test_booking_form_requires_typed_client_side_validation():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert "const enquiryFieldRules = {" in template
    assert 'pattern="[A-Za-z][A-Za-z .\'\\-]{1,79}"' in template
    assert 'pattern="\\+?[0-9][0-9 ()\\-]{8,18}[0-9]"' in template
    assert 'id="lead_email" name="email" type="email"' in template
    assert 'autocomplete="email" maxlength="120"' in template
    assert 'id="lead_device_count" name="device_count" type="number" min="1" max="10000" step="1" inputmode="numeric"' in template
    assert 'textarea id="lead_message" name="message" minlength="10" maxlength="800"' in template
    assert "field.setCustomValidity(message)" in template
    assert "enquirySubmitButton.disabled = !isReady" in template
    assert 'class="field-warning" id="lead_phone_warning"' in template


def test_flask_static_assets_have_cache_and_compression_support():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    service_worker = (PROJECT_ROOT / "flask_app" / "static" / "service-worker.js").read_text(encoding="utf-8")

    assert 'app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=30)' in server_source
    assert '"Cache-Control", "public, max-age=2592000, immutable"' in server_source
    assert "def should_gzip_response(response):" in server_source
    assert "gzip.compress(payload, compresslevel=6)" in server_source
    assert 'const CACHE_NAME = "swt-pwa-v6";' in service_worker
    assert "/static/marketing/smart-water-tank-hero-ai-1280.webp" in service_worker


def test_sales_enquiry_server_validation_matches_booking_form_rules():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'valid_segments = {' in server_source
    assert 're.fullmatch(r"[A-Za-z][A-Za-z .\'-]*", cleaned["name"])' in server_source
    assert 'len(cleaned["email"]) > 120' in server_source
    assert 're.fullmatch(r"\\+?[0-9][0-9 ()-]*[0-9]", cleaned["phone"])' in server_source
    assert 'len(phone_digits) > 15' in server_source
    assert 'cleaned["segment"] not in valid_segments' in server_source
    assert 'len(cleaned["message"]) < 10' in server_source


def test_admin_customer_page_uses_single_relay_alert_cleanup_hook():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def resolve_transient_relay_alerts" in server_source
    assert "message LIKE 'Cloud relay returned HTTP 5%'" in server_source
    assert "set_alert(\"relay_failure\", \"warning\", f\"Cloud relay returned HTTP {response.status_code}.\", active=True)" not in server_source


def test_admin_device_list_reconciles_alerts_from_current_feed():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def refresh_admin_entry_alerts" in server_source
    assert "refresh_admin_entry_alerts(entry)" in server_source
    refresh_call = server_source.index("refresh_admin_entry_alerts(entry)")
    summaries_call = server_source.index("fetch_active_alert_summaries(", refresh_call)
    assert refresh_call < summaries_call


def test_admin_dashboard_hides_alerts_older_than_24_hours():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def admin_alert_cutoff_timestamp(hours=24)" in server_source
    assert "def resolve_admin_expired_alerts" in server_source
    assert "WHERE active = 1" in server_source
    assert "AND updated_at < ?" in server_source
    assert "fetch_filtered_alerts(limit=10, updated_since=alert_cutoff)" in server_source
    assert "fetch_active_alert_device_ids(updated_since=admin_alert_cutoff_timestamp())" in server_source
    assert "updated_since=admin_alert_cutoff_timestamp()" in server_source


def test_device_event_feed_uses_actionable_health_events():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS device_events" in server_source
    assert "event_key TEXT NOT NULL UNIQUE" in server_source
    assert "def persist_device_events" in server_source
    assert "def fetch_device_events" in server_source
    assert "def sync_device_events" in server_source
    assert "sync_device_events(device_id=cleaned.get(\"device_id\"))" in server_source
    assert "def build_snapshot_activity_events" in server_source
    assert "def merge_activity_events" in server_source
    assert "build_generated_device_events(" in server_source
    assert "merge_activity_events(generated_events, local_log_events, stored_events, limit=normalized_limit)" in server_source
    assert "build_snapshot_activity_events(limit=normalized_limit, device_id=device_id)" in server_source

    for event_kind in (
        "telemetry_recovered",
        "pump_no_level_rise",
        "source_tank_low",
        "source_tank_recovered",
        "wifi_signal_weak",
        "wifi_signal_recovered",
        "wifi_signal_drop",
        "wifi_signal_improved",
        "wifi_disconnect_frequency_high",
        "sensor_recovered",
        "heap_low",
        "heap_recovered",
        "firmware_changed",
        "config_changed",
        "device_rebooted",
        "reboot_frequency_high",
        "command_queued",
        "command_acknowledged",
        "command_delivery_failed",
        "ota_published",
        "ota_succeeded",
        "ota_failed",
    ):
        assert event_kind in server_source


def test_project_device_env_can_disable_local_relay_defaults():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '"RELAY_STATUS_URLS"' in server_source
    assert '"RELAY_COMMAND_URLS"' in server_source
    assert "blank_overrides.add(key)" in server_source
    assert "if key in blank_overrides:" in server_source


def test_project_device_env_can_override_smtp_settings():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    device_env_example = (PROJECT_ROOT / "device.env.example").read_text(encoding="utf-8")

    assert "DEVICE_ENV_OVERRIDE_KEYS" in server_source
    assert '"SMTP_PASSWORD"' in server_source
    assert "forced_overrides.add(key)" in server_source
    assert "if key in forced_overrides:" in server_source
    assert "SMTP_HOST=mail.salewell.co.in" in device_env_example
    assert "SMTP_PASSWORD=replace-with-support-mailbox-password" in device_env_example


def test_admin_slave_status_uses_direct_peer_packet_freshness():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "DIRECT_PEER_STALE_AFTER_SECONDS" in server_source
    assert "def direct_peer_packet_is_fresh" in server_source
    assert "peer_packet_fresh = direct_peer_packet_is_fresh(entry)" in server_source
    assert "slave_upper_enabled = bool(service_config.get(\"slave_upper_sensor_enabled\"))" in server_source
    assert "upper_reachable = online and peer_packet_fresh is True and tank_level_is_valid" in server_source
    assert '"direct_peer_last_packet_age_s": payload.get("direct_peer_last_packet_age_s")' in server_source
    assert 'cleaned.get("direct_peer_last_packet_age_s")' in server_source
    assert '"direct_peer_last_packet_age_s": "INTEGER"' in server_source


def test_admin_dashboard_maps_municipal_sensor_snapshot_fields():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '"municipal_sensor_enabled": payload.get("municipal_sensor_enabled")' in server_source
    assert '"municipal_sensor_state": payload.get("municipal_sensor_state")' in server_source
    assert '"municipal_sensor_simulated": payload.get("municipal_sensor_simulated")' in server_source
    assert '"municipal_sensor_reachable": payload.get("municipal_sensor_reachable")' in server_source
    assert '"municipal_sensor_last_updated": payload.get("municipal_sensor_last_updated")' in server_source


def test_admin_device_table_keeps_echo_data_available_but_shows_only_tank_level():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert '"sensor_distance_cm": payload.get("sensor_distance_cm")' in server_source
    assert '"sensor_distance_label": sensor_distance_label' in server_source
    assert '"water_depth_label": payload.get("water_depth_label")' in server_source
    assert "Depth {{ device.water_depth_label" not in admin_template
    assert "Echo {{ device.sensor_distance_label" not in admin_template
    assert 'data-device-field="tank_level"' in admin_template
    assert '"%.1f"|format(device.sensor_distance_cm)' not in admin_template


def test_admin_customer_page_renders_string_sensor_distance():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    client = server.app.test_client()
    with client.session_transaction() as session:
        auth_marker = server.current_auth_marker_for_identity("admin", username=server.LOGIN_USERNAME)
        platform_session_id = server.register_active_platform_session(
            server.SESSION_PLATFORM_DASHBOARD,
            "admin",
            username=server.LOGIN_USERNAME,
        )
        session["logged_in"] = True
        session["username"] = server.LOGIN_USERNAME
        session["role"] = "admin"
        session["device_id"] = None
        session["auth_marker"] = auth_marker
        session["platform_session_id"] = platform_session_id
        session["admin_logged_in"] = True
        session["admin_username"] = server.LOGIN_USERNAME
        session["admin_device_id"] = None
        session["admin_auth_marker"] = auth_marker
        session["admin_platform_session_id"] = platform_session_id
        session["csrf_token"] = "test-csrf-token"
    response = client.get("/admin/customers")
    assert response.status_code == 200


def test_admin_lower_sensor_status_uses_saved_enable_flag_not_raw_off_snapshot():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    fields = server.admin_relay_sensor_status_fields(
        {
            "telemetry_status": "live",
            "lower_sensor": "OFF",
        },
        {"source_tank_monitoring_enabled": True},
    )

    assert fields["lower_sensor_status_label"] == "Unreachable"
    assert fields["lower_sensor_status_tone"] == "offline"


def test_admin_municipal_sensor_status_uses_reachability_labels_for_simulator():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    simulated_fields = server.admin_municipal_sensor_status_fields(
        {
            "telemetry_status": "live",
            "municipal_sensor_enabled": True,
            "municipal_sensor_simulated": True,
            "municipal_sensor_reachable": True,
            "municipal_sensor_state": "available",
        },
        {"municipal_sensor_enabled": True},
    )
    unreachable_fields = server.admin_municipal_sensor_status_fields(
        {
            "telemetry_status": "live",
            "municipal_sensor_enabled": True,
            "municipal_sensor_simulated": False,
            "municipal_sensor_reachable": False,
            "municipal_sensor_state": "unknown",
        },
        {"municipal_sensor_enabled": True},
    )

    assert simulated_fields == ("Reachable", "online")
    assert unreachable_fields == ("Unreachable", "offline")


def test_dashboards_render_company_icon_home_links():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'class="dashboard-brand-link" href="/"' in admin_template
    assert 'id="brandLogo" class="brand-logo" href="/"' in customer_template
    assert "url_for('homepage')" not in customer_template


def test_web_pages_use_short_private_cache_while_live_endpoints_stay_no_store():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    pwa_head = (PROJECT_ROOT / "flask_app" / "templates" / "_pwa_head.html").read_text(encoding="utf-8")
    smooth_navigation = (PROJECT_ROOT / "flask_app" / "static" / "js" / "smooth-navigation.js").read_text(encoding="utf-8")
    service_worker = (PROJECT_ROOT / "flask_app" / "static" / "service-worker.js").read_text(encoding="utf-8")

    assert "DYNAMIC_HTML_CACHE_SECONDS" in server_source
    assert "private, max-age=" in server_source
    assert "stale-while-revalidate" in server_source
    assert "response.add_etag(weak=True)" in server_source
    assert '"/device/command"' in server_source
    assert '"/api/"' in server_source
    assert "Cache-Control\", \"no-store\"" in server_source
    assert 'cache: "default"' in smooth_navigation
    assert 'cache: "no-store"' not in smooth_navigation
    assert "20260615-cache-v1" in pwa_head
    assert '"/static/js/smooth-navigation.js"' in service_worker
    assert 'const CACHE_NAME = "swt-pwa-v6";' in service_worker


def test_customer_graphs_refresh_after_live_telemetry_changes():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert "const ANALYTICS_LIVE_REFRESH_MS=30000;" in customer_template
    assert "Date.now()-state.analyticsUpdatedAt>ANALYTICS_LIVE_REFRESH_MS" in customer_template
    assert "loadAnalytics({force:true})" in customer_template


def test_admin_customer_auto_refresh_uses_json_not_full_page_downloads():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '@app.route("/admin/customers/device-table.json")' in server_source
    assert "def admin_customers_device_table_json():" in server_source
    assert 'new URL("/admin/customers/device-table.json", window.location.origin)' in admin_template
    assert "DOMParser().parseFromString" not in admin_template
    refresh_start = admin_template.index("async function refreshDeviceTable")
    refresh_body = admin_template[refresh_start : admin_template.index("if (input) input.addEventListener", refresh_start)]
    assert "response.text()" not in refresh_body
    assert "response.json()" in refresh_body
    assert 'data-device-field="master_status"' in admin_template
    assert 'data-device-field="last_sync"' in admin_template
    assert 'data-summary-field="online_devices"' in admin_template
    assert "updateSummaryFromJson(payload.summary)" in refresh_body
    assert '"summary": device_summary' in server_source


def test_interval_polling_avoids_heavy_page_and_analytics_downloads():
    dashboard_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert "setInterval(()=>loadAnalytics()" not in dashboard_template
    assert "ANALYTICS_STALE_MS" in dashboard_template
    assert 'const ANALYTICS_CACHE_SCHEMA_VERSION="v5-reliable-drawdown";' in dashboard_template
    assert "function analyticsCacheContext()" in dashboard_template
    assert "function analyticsHasChartData(data)" in dashboard_template
    assert "function analyticsIsFallbackPayload(data)" in dashboard_template
    assert "function analyticsIsStrongPayload(data)" in dashboard_template
    assert "state.lastGoodAnalytics={key,data:merged};" in dashboard_template
    assert "return analyticsIsStrongPayload(data);" in dashboard_template
    assert "const chartData=analyticsChartsPayload(data);" in dashboard_template
    assert "data=chartData;" in dashboard_template
    assert "const merged=analyticsChartsPayload(data);" in dashboard_template
    assert "analyticsIsFallbackPayload(payload)" in dashboard_template
    assert "if(!analyticsIsStrongPayload(cached.data))" in dashboard_template
    assert "Date.now()-savedAt>ANALYTICS_CACHE_MAX_AGE_MS" in dashboard_template
    assert "const tasks=[refreshLive({force}),refreshEvents({force})]" in dashboard_template
    assert "const visibleHeight=Math.max(rawHeight,Math.abs(value)<=0.0001?2:1);" in dashboard_template
    assert "DEVICE_HEAVY_REFRESH_MS" not in device_template
    assert "const includeHeavy=!silent&&!latestHistory.length;" in device_template


def test_customer_dashboard_spins_company_logo_while_pump_is_on_or_start_pending():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert ".brand-logo.is-spinning .logo-wheel" in customer_template
    assert "const PUMP_START_GRACE_MS=60000;" in customer_template
    assert "pendingPumpStartUntil:0" in customer_template
    assert "const pendingActive=pending&&Date.now()<state.pendingPumpStartUntil" in customer_template
    assert "const spinning=running||pendingActive||startInferred" in customer_template
    assert "state.pendingPumpStartUntil=startRequest?Date.now()+PUMP_START_GRACE_MS:0" in customer_template


def test_customer_dashboard_stop_button_uses_effective_running_state():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    update_start = customer_template.index("function updateCommandAvailability")
    update_body = customer_template[update_start : customer_template.index("function renderCustomerOverview", update_start)]

    assert "const pendingStartActive=state.pendingPumpStart&&Date.now()<state.pendingPumpStartUntil;" in update_body
    assert "const running=isPumpRunning(snapshot?.motor)||pendingStartActive||state.upperTankIncreasing;" in update_body
    assert 'nodesById("btn_off").forEach((button)=>{button.disabled=!baseEnabled||!running;});' in update_body


def test_customer_dashboard_motor_chart_shows_stepped_digital_state():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert "function buildMotorActivitySegments" in customer_template
    assert "function drawMotorActivityTimeline" in customer_template
    assert "drawMotorActivityTimeline(charts.motor.canvas,\"Motor State\",motorSeriesRaw.time,motorSeriesRaw.values" in customer_template
    assert 'drawCanvasSeries(charts.motor.canvas,"Motor State"' not in customer_template
    assert "function formatDurationSeconds(value)" in customer_template
    assert "pumpActivity?.avg_run_seconds" in customer_template
    assert 'xLabelMode:"datetime"' in customer_template
    assert '--chart-motor-on:#22c55e' in customer_template
    assert '--chart-motor-off:#94a3b8' in customer_template
    assert 'const onColor=themeVar("--chart-motor-on")' in customer_template
    assert 'const offColor=themeVar("--chart-motor-off")' in customer_template
    assert 'const barColor=(state)=>state===1?onColor:offColor;' in customer_template
    assert 'const summaryParts=["Green ON","Gray OFF"];' in customer_template
    assert 'ctx.fillText("1 (ON)",box.left-10,highY+4);' in customer_template
    assert 'ctx.fillText("0 (OFF)",box.left-10,lowY+4);' in customer_template
    assert 'ctx.lineTo(endX,stateY(next.state));' in customer_template
    assert 'const tickCount=Math.max(2,Math.min(5,Math.round(box.plotWidth/170)));' in customer_template
    assert 'function isCustomerWaterEvent(event)' in customer_template
    assert 'Level history hidden because sensor changes failed validation.' in customer_template
    assert 'label==="Today"?"Today’s water activity":"Water activity over the selected period"' in customer_template


def test_dashboard_prioritizes_live_operations_and_explains_advanced_details():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    sticky_start = customer_template.index('<nav class="ops-sticky"')
    sticky_end = customer_template.index("</nav>", sticky_start)
    assert customer_template.rfind("{% if is_admin %}", 0, sticky_start) > customer_template.rfind("{% endif %}", 0, sticky_start)
    assert customer_template.index("{% endif %}", sticky_end) > sticky_end
    assert 'id="stickyTankLevel"' in customer_template
    assert 'id="stickyPumpStatus"' in customer_template
    assert 'id="stickyAiAlert"' in customer_template
    assert 'id="systemHealthScore"' in customer_template
    assert 'id="motorConfirmDialog"' in customer_template
    assert "function closeMotorConfirmation(confirmed)" in customer_template
    assert "executeMotorCommand(request.path,request.label)" in customer_template
    assert 'id="dashboardSettings"' in customer_template
    assert '<details id="dashboardSettings"' in customer_template
    assert "function eventPresentation(event)" in customer_template
    assert 'showValues:true' in customer_template
    assert 'highlightPeak:true' in customer_template
    assert "eventMarkers:levelEvents" in customer_template
    assert 'id="viewLeakReportButton"' in customer_template
    assert "aiLeakConfidence>90" in customer_template


def test_customer_usage_cards_show_live_values_while_history_confidence_builds():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    usage_start = customer_template.index("function updateCustomerUsageCards(")
    usage_end = customer_template.index("function updateCustomerSpotlightCards", usage_start)
    usage_body = customer_template[usage_start:usage_end]

    assert 'setText("usage_change",displayedLiters!==null?' in usage_body
    assert "usage_physically_plausible!==false" in usage_body
    assert 'setText("customerUsageMetricLabel",todayRange?"Water used today":"Observed water use")' in usage_body
    assert "Hidden because sensor changes exceed the water supported by observed refill cycles." in usage_body
    assert 'Number(snapshot.tomorrow_prediction)' in usage_body
    assert 'Number(snapshot.ai_usage_rate)' in usage_body
    assert '"Live device estimate; historical confidence is still building."' in usage_body
    assert '"Live device usage-rate estimate."' in usage_body
    assert "updateCustomerUsageCards(snapshot,analytics);" in customer_template
    assert "function customerUsageSavings(" in customer_template
    assert 'value:"0.0 L"' in customer_template
    assert 'const ANALYTICS_CACHE_SCHEMA_VERSION="v5-reliable-drawdown";' in customer_template
    assert "function analyticsReliabilityNote(" in customer_template


def test_customer_dashboard_avoids_duplicate_summary_cards():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert '<section class="panel customer-only customer-summary-panel"' not in customer_template
    assert '<div class="eyebrow">Pump Section</div>' not in customer_template
    assert 'id="customerHeroTankLevel"' not in customer_template
    assert 'id="customerConfidenceScore"' not in customer_template
    assert 'id="heroConnectionStatus"' not in customer_template
    assert 'id="customerConfidenceGuidance"' not in customer_template
    assert '<span>Current state</span><strong id="motor">' in customer_template
    assert '<span>Mode</span><strong id="mode">' in customer_template
    assert 'id="customerUsageMetricLabel"' in customer_template
    assert 'id="ai_tomorrow_usage"' in customer_template
    assert 'id="consumption_rate"' in customer_template
    assert 'id="customerSavingsNow"' in customer_template


def test_customer_ai_analysis_tracks_selected_range_and_defaults_to_today():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert "const DEFAULT_ANALYTICS_RANGE_DAYS=1;" in customer_template
    assert 'quickRange:DEFAULT_ANALYTICS_RANGE_DAYS' in customer_template
    assert 'id="range_1" class="btn-lite active"' in customer_template
    assert "function updateAnalyticsRangeContext(" in customer_template
    assert "function beginAnalyticsRangeChange()" in customer_template
    assert 'updateCustomerSpotlightCards({snapshot:state.data.snapshot,system:state.data.systemStatus,analytics:null})' in customer_template
    assert 'const requestQuery=query();' in customer_template
    assert 'if(state.inFlight.analytics){if(state.analyticsRequestQuery===requestQuery)return;state.controllers.analytics?.abort();}' in customer_template
    for range_id in (
        "customerUsageRange",
        "customerGuidanceRange",
        "levelChartRange",
        "dailyChartRange",
        "patternChartRange",
        "motorChartRange",
    ):
        assert f'id="{range_id}"' in customer_template


def test_admin_fleet_page_has_actionable_triage_and_reliable_filters():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'class="searchBar fleet-toolbar-sticky"' in admin_template
    for filter_name in ("all", "online", "offline", "critical", "warning", "recent", "poor-signal", "telemetry-missing"):
        assert f'data-quick-filter="{filter_name}"' in admin_template
    assert 'id="customer_filter"' in admin_template
    assert 'data-resolve-alert="{{ lead_alert.id }}"' in admin_template
    assert 'data-device-field="health_score"' not in admin_template
    assert 'data-last-seen-age=' in admin_template
    assert 'id="export_visible_devices"' in admin_template
    assert 'id="device_table_top_scroll"' in admin_template
    assert 'id="device_table_scroll"' in admin_template
    assert 'id="device_page_status"' in admin_template
    assert 'id="device_page_first"' in admin_template
    assert 'id="device_page_previous"' in admin_template
    assert 'id="device_page_next"' in admin_template
    assert 'id="device_page_last"' in admin_template
    assert "function syncDeviceTableScrollerWidth()" in admin_template
    assert '<th>Actions</th>' not in admin_template
    assert 'class="device-actions"' not in admin_template
    assert 'data-device-field="pump_mode"' not in admin_template
    assert 'data-device-field="depth_echo"' not in admin_template
    assert 'class="last-seen-indicator"' in admin_template
    assert "connectivityLabel(device.master_status" in admin_template
    assert "&#9989; No Active Alerts" in admin_template
    assert 'data-summary-field="online_percent"' not in admin_template
    assert 'class="summary-percent"' not in admin_template
    assert "priorityDifference" in admin_template
    assert "function formatAgeSeconds(seconds)" in admin_template
    assert "def admin_device_health_fields(entry):" in server_source
    assert '"health_score": int(device.get("admin_health_score") or 0)' in server_source
    assert 'item.get("firmware_version")' in server_source


def test_device_detail_reduces_density_and_keeps_critical_actions_safe():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert 'class="device-action-bar"' in template
    for section_id in ("device-overview", "device-memory", "device-configuration", "device-firmware", "device-logs"):
        assert f'id="{section_id}"' in template
    assert 'id="deviceHealthScore"' in template
    assert 'id="lastSeenAge"' in template
    assert 'id="firmwareVersion"' in template
    assert 'id="rssiSignal"' in template
    assert 'id="deviceUptime"' in template
    assert 'data-info-tab="network"' in template
    assert 'data-info-tab="sensors"' in template
    assert 'type="range" min="0" max="95"' in template
    assert 'id="activitySearch"' in template
    assert 'id="activitySeverity"' in template
    assert 'id="activityPause"' in template
    assert 'id="activityCopy"' in template
    assert 'id="activityDownload"' in template
    assert 'data-confirm-title="Upload master firmware?"' in template
    assert 'data-confirm-title="Upload slave firmware?"' in template
    assert 'document.getElementById("deviceRestartButton")' in template
    assert "function refreshLastSeenAge()" in template


def test_dashboard_titles_are_simple_and_icon_precedes_title():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert '<h1 class="hero-title">Dashboard</h1>' in admin_template
    assert "<h1>Dashboard</h1>" in customer_template
    assert admin_template.index('class="dashboard-brand-link"') < admin_template.index('<h1 class="hero-title">Dashboard</h1>')
    assert customer_template.index('id="brandLogo"') < customer_template.index("<h1>Dashboard</h1>")
    assert "Admin Dashboard</h1>" not in admin_template
    assert "Home Water Dashboard" not in customer_template
    assert "Home Water Dashboard" in login_template


def test_device_detail_dashboard_button_returns_to_admin_dashboard():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert '<a class="btn" href="{{ url_for(\'admin_customers\') }}">Dashboard</a>' in device_template
    assert '<a class="btn" href="/">Dashboard</a>' not in device_template


def test_device_detail_exposes_mobile_logout_button():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'action="/devices/{{ device_id }}/mobile/logout" data-ajax-form' in device_template
    assert "Log Out Mobile Devices" in device_template
    assert 'data-confirm-title="Log out mobile devices?"' in device_template
    assert '@app.route("/devices/<device_id>/mobile/logout", methods=["POST"])' in server_source
    assert "def admin_device_detail_mobile_logout(device_id):" in server_source


def test_device_detail_exposes_android_firmware_upgrade_trigger():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'url_for(\'admin_device_detail_mobile_firmware_upgrade\', device_id=device_id)' in device_template
    assert "Android App OTA Trigger" in device_template
    assert "Queue Android OTA" in device_template
    assert 'data-ajax-form data-confirm-title="Queue Android firmware upgrade?"' in device_template
    assert '@app.route("/devices/<device_id>/mobile/firmware-upgrade", methods=["POST"])' in server_source
    assert "def admin_device_detail_mobile_firmware_upgrade(device_id):" in server_source


def test_device_detail_shows_android_sso_server_session_diagnostic():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "android_sso_active_session_count" in device_template
    assert "Server currently allows" in device_template
    assert "def active_platform_session_count(" in server_source
    assert "android_sso_active_session_count=active_platform_session_count(" in server_source


def test_device_detail_uses_compact_balanced_cards_and_buttons():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert ".btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border-radius:12px;border:1px solid var(--button-line);text-decoration:none;font-weight:800;background:transparent;color:var(--text);font-family:inherit;min-width:0;width:auto}" in device_template
    assert ".btn-full{width:auto}" in device_template
    assert ".hero-actions .btn{height:36px;min-height:36px;min-width:0;padding:0 13px;border-radius:10px;font-size:12px}" in device_template
    assert ".summary-grid{grid-template-columns:repeat(5,minmax(0,1fr));align-items:stretch}" in device_template
    assert ".admin-grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));margin-top:16px;align-items:start}" in device_template
    assert ".config-sections{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));align-items:start}" in device_template
    assert ".firmware-upload-grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:16px;align-items:start}" in device_template
    assert ".admin-form>form{display:grid;gap:10px;width:100%}" in device_template
    assert ".admin-grid .admin-form:not(.config-form)>.btn,.admin-grid .admin-form:not(.config-form)>form .btn,.firmware-upload-grid .btn{width:100%;justify-self:stretch}" in device_template
    assert ".admin-grid .config-form>.btn{justify-self:end;width:min(100%,280px);max-width:none}" in device_template
    assert ".sensor-setup-card{align-content:start}" in device_template
    assert ".tank-setup-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;width:100%}" in device_template
    assert ".tank-setup-actions .btn{width:100%;justify-self:stretch}" in device_template
    assert ".summary-value.tone-ok,.summary-value.tone-warn,.summary-value.tone-bad,.summary-value.tone-info" in device_template
    assert ".hero-actions form,.hero-actions .btn{width:auto}" in device_template
    assert 'document.querySelectorAll("[data-ajax-form]")' in device_template
    assert ".activity-pagination .btn{min-width:0;width:auto;padding:0 12px;font-size:15px}" in device_template
    assert ".confirm-actions .btn{min-width:0}" in device_template
    assert ".admin-form .btn,.config-form>.btn{width:100%;justify-self:stretch;max-width:none}" in device_template


def test_device_detail_upload_result_uses_closable_popup():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'data-firmware-upload-form' in device_template
    assert 'modal.dataset.resultModal=""' in device_template
    assert 'request.setRequestHeader("X-Requested-With","XMLHttpRequest")' in device_template
    assert "showResultModal(ok?\"Upload successful\":\"Upload failed\"" in device_template
    assert "const INITIAL_CONFIG_MESSAGE={{ config_message|tojson }};" in device_template
    assert "const INITIAL_CONFIG_ERROR={{ config_error|tojson }};" in device_template
    assert "function showInitialConfigResult()" in device_template
    assert 'request.headers.get("X-Requested-With") == "XMLHttpRequest"' in server_source
    assert '<div class="message success" style="margin-top:14px">{{ config_message }}</div>' not in device_template
    assert '<div class="message error" style="margin-top:14px">{{ config_error }}</div>' not in device_template
    assert "{% if config_message or config_error %}" not in device_template


def test_device_detail_exposes_admin_tank_setup_controls():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "{{ upper_setup_label }} Sensor Setup" in device_template
    assert "Lower / Source Sensor Setup" in device_template
    assert 'id="upperSensorSetupSection"' in device_template
    assert 'id="lowerSensorSetupSection"' in device_template
    assert 'id="lowerSensorOption" type="checkbox" name="source_tank_monitoring_enabled"' in device_template
    assert 'onchange="window.swtSyncLowerSensorSetupVisibility&&window.swtSyncLowerSensorSetupVisibility()"' in device_template
    assert '{% set upper_setup_label = "Slave Upper" if slave_upper_checked else "Master Upper" %}' in device_template
    assert "{% if master_upper_checked or slave_upper_checked %}" in device_template
    assert 'class="admin-form sensor-setup-card {% if not service_config.get("source_tank_monitoring_enabled") %}hidden-section{% endif %}" id="lowerSensorSetupSection"' in device_template
    assert "upperTankSetupForm" in device_template
    assert "lowerTankSetupForm" in device_template
    assert 'id="upperTankSetupForm" method="post" action="{{ url_for(\'admin_device_detail_sensor_configure\', device_id=device_id) }}" data-ajax-form' in device_template
    assert 'id="lowerTankSetupForm" method="post" action="{{ url_for(\'admin_device_detail_sensor_configure\', device_id=device_id) }}" data-ajax-form' in device_template
    assert "admin_upper_tank_height_cm" in device_template
    assert "admin_upper_tank_capacity_liters" in device_template
    assert "admin_lower_tank_height_cm" in device_template
    assert "admin_lower_tank_capacity_liters" in device_template
    assert '{% set upper_tank_height_value = current_saved_config.get("upper_tank_height_cm")' in device_template
    assert '{% set lower_tank_capacity_value = current_saved_config.get("lower_tank_capacity_liters")' in device_template
    assert 'value="{{ upper_tank_height_value }}" placeholder="60.0"' in device_template
    assert 'value="{{ upper_tank_capacity_value }}" placeholder="1000"' in device_template
    assert 'value="{{ lower_tank_height_value }}" placeholder="60.0"' in device_template
    assert 'value="{{ lower_tank_capacity_value }}" placeholder="1000"' in device_template
    assert "upperTankSetupCalibrateButton" in device_template
    assert "lowerTankSetupCalibrateButton" in device_template
    assert 'class="tank-setup-actions"' in device_template
    assert "document.querySelectorAll(\".tank-setup-form\")" in device_template
    assert "function syncLowerSensorSetupVisibility()" in device_template
    assert 'lowerSetup.classList.toggle("hidden-section",!lowerSensor.checked);' in device_template
    assert "window.swtSyncLowerSensorSetupVisibility=syncLowerSensorSetupVisibility;" in device_template
    assert "syncLowerSensorSetupVisibility();" in device_template
    assert 'window.addEventListener("pageshow",syncLowerSensorSetupVisibility);' in device_template
    assert 'const submitter=event.submitter||null;' in device_template
    assert 'form.requestSubmit(submitter||undefined);' in device_template
    assert 'heightInput&&heightInput.value.trim()!==""' in device_template
    assert 'capacityInput&&capacityInput.value.trim()!==""' in device_template
    assert "hasHeight&&hasCapacity&&heightInput.checkValidity()&&capacityInput.checkValidity()" in device_template
    assert 'if(button?.name&&!formData.has(button.name))formData.append(button.name,button.value||"");' in device_template
    assert "Save Height & Capacity" in device_template
    assert ">Calibrate Sensor</span>" in device_template
    assert 'data-confirm-button="Calibrate Sensor"' in device_template
    assert "admin_device_detail_sensor_configure" in device_template
    assert "admin_device_detail_sensor_calibrate" in server_source
    assert 'name="sensor" value="upper"' in device_template
    assert 'name="sensor" value="lower"' in device_template
    assert 'data-confirm-title="Save and calibrate {{ upper_setup_label|lower }} sensor?"' in device_template
    assert 'command = f"CONFIG_LOWER:{height_cm:.1f}:{capacity_liters:.1f}"' in server_source
    assert 'f"CONFIG_UPPER:{height_cm:.1f}:{capacity_liters:.1f}"' in server_source
    assert 'command = f"CONFIG_CAPACITY:{capacity_liters:.1f}"' in server_source
    assert 'error="Tank height is required before calibration."' in server_source
    assert "device_detail_action_response(" in server_source
    assert 'calibration_command = "CALIBRATE_LOWER" if lower_requested else "CALIBRATE_UPPER"' in server_source
    assert 'command = "CALIBRATE_LOWER" if lower_requested else "CALIBRATE_UPPER"' in server_source
    assert "The master will forward upper calibration to the slave MCU" in server_source


def test_device_detail_keeps_upper_sensor_sources_mutually_exclusive():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '{% set slave_upper_checked = slave_device_checked and service_config.get("slave_upper_sensor_enabled", slave_device_checked) %}' in device_template
    assert '{% set master_upper_checked = ((not slave_upper_checked) and service_config.get("master_upper_sensor_enabled", not slave_device_checked)) or not slave_upper_checked %}' in device_template
    assert 'id="masterUpperSensorOption" type="radio" name="upper_sensor_source" value="master" {% if master_upper_checked %}checked{% endif %} onchange="window.swtSyncRuntimeConfigurationOptions&&window.swtSyncRuntimeConfigurationOptions()"' in device_template
    assert 'id="slaveUpperSensorOption" type="radio" name="upper_sensor_source" value="slave" {% if slave_upper_checked %}checked{% endif %} onchange="window.swtSyncRuntimeConfigurationOptions&&window.swtSyncRuntimeConfigurationOptions()"' in device_template
    assert 'Only one upper sensor source can be active.' in device_template
    assert 'onchange="window.swtSyncRuntimeConfigurationOptions&&window.swtSyncRuntimeConfigurationOptions()"' in device_template
    assert "function syncRuntimeConfigurationOptions()" in device_template
    assert "function syncUpperSensorSetupLabels()" in device_template
    assert 'const label=slaveUpper?.checked?(section.dataset.slaveUpperLabel||"Slave Upper"):(section.dataset.masterUpperLabel||"Master Upper");' in device_template
    assert 'if(saveButton)saveButton.textContent="Save Height & Capacity";' in device_template
    assert 'calibrateButton.dataset.confirmButton="Calibrate Sensor";' in device_template
    assert 'if(span)span.textContent="Calibrate Sensor";' in device_template
    assert "window.swtSyncRuntimeConfigurationOptions=syncRuntimeConfigurationOptions;" in device_template
    assert 'window.addEventListener("pageshow",syncRuntimeConfigurationOptions);' in device_template
    assert "slaveUpper.checked=false;" in device_template
    assert "masterUpper.checked=false;" in device_template
    assert "slaveUpper.disabled=true;" in device_template
    assert "slaveUpper.disabled=false;" in device_template
    assert 'upper_sensor_source = request.form.get("upper_sensor_source")' in server_source
    assert 'if upper_sensor_source in {"master", "slave"}:' in server_source
    assert 'slave_upper_sensor_enabled = slave_device_enabled and upper_sensor_source == "slave"' in server_source
    assert 'elif slave_upper_sensor_enabled:' in server_source
    assert 'master_upper_sensor_enabled = False' in server_source


def test_device_detail_renders_master_slave_memory_health_graph():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    firmware_source = (PROJECT_ROOT.parent / "swt_firmware_project" / "src" / "two_node_udp.cpp").read_text(encoding="utf-8")

    assert "Memory Health" in device_template
    assert 'id="memoryChartWrap"' in device_template
    assert "function renderMemoryHealth(history,snapshot,serviceConfig)" in device_template
    assert "snapshotHeap=Number(snapshot?.free_heap)" in device_template
    assert "snapshotSlaveHeap=Number(snapshot?.slave_free_heap)" in device_template
    assert "const usesSlave=Boolean(serviceConfig?.slave_device_enabled)" in device_template
    assert 'const slaveStat=usesSlave?' in device_template
    assert 'const slaveLegend=usesSlave?' in device_template
    assert "Master CPU" not in device_template
    assert "Slave CPU" not in device_template
    assert "memory-line-master" in device_template
    assert "memory-line-slave" in device_template
    assert "health-chart-grid" not in device_template
    assert 'title:"Heap Graph"' in device_template
    assert 'title:"CPU Graph"' not in device_template
    assert "chart-series-legend" in device_template
    assert "Master + Slave" in device_template
    assert "memory-live-wave" not in device_template
    assert "wavePathFor" not in device_template
    assert "let latestHistory=[]" in device_template
    assert "renderMemoryHealth(latestHistory,snapshot,serviceConfig)" in device_template
    assert 'statusUrl.searchParams.set("history","0")' in device_template
    assert "slave_free_heap" in server_source
    assert "slave_cpu_utilization_pct" in server_source
    assert "cpu_utilization_pct REAL" in server_source
    assert '"free_heap": row["free_heap"]' in server_source
    assert '"slave_free_heap": row["slave_free_heap"]' in server_source
    assert "fetch_device_history(scoped_device_id, limit=48)" in server_source
    assert 'doc["slave_free_heap"] = lastSlaveFreeHeap' in firmware_source
    assert 'doc["cpu_utilization_pct"] = cpuUtilizationPct' in firmware_source
    assert "const uint32_t freeHeapBeforeStatusJson = ESP.getFreeHeap();" in firmware_source
    assert 'doc["free_heap"] = freeHeapBeforeStatusJson' in firmware_source


def test_android_release_upload_modal_is_detached_and_shows_progress():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert 'data-android-upload-form' in admin_template
    assert 'data-android-upload-progress' in admin_template
    assert 'id="upload_result_panel"' in admin_template
    assert 'data-upload-result-message' in admin_template
    assert 'showUploadResultPanel("Upload failed"' in admin_template
    assert 'showUploadResultPanel("Upload successful"' in admin_template
    assert 'document.write(request.responseText)' not in admin_template[
        admin_template.index('const androidUploadForm = document.querySelector("[data-android-upload-form]")')
        : admin_template.index('const registerDeviceForm = document.querySelector("[data-register-device-form]")')
    ]
    assert 'request.upload.addEventListener("progress"' in admin_template
    assert 'document.body.appendChild(panel)' in admin_template
    assert 'panel.dataset.busy === "true"' in admin_template
    assert '.android-upload-progress-bar' in admin_template


def test_android_release_upload_allows_debug_or_unsigned_apks():
    android_release_helper = (PROJECT_ROOT / "flask_app" / "android_releases.py").read_text(encoding="utf-8")

    assert "ANDROID_BLOCKED_APK_FILENAME_MARKERS" not in android_release_helper
    assert "Debug or unsigned APK files are not allowed" not in android_release_helper


def test_android_release_cleanup_removes_all_builds_copy():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert "Remove All Android Builds" in admin_template
    assert "Remove every uploaded Android build record and APK file" in admin_template
    assert "Latest Android build will stay active" not in admin_template


def test_release_channel_keeps_firmware_on_device_detail_page():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    mobile_firmware_routes = (PROJECT_ROOT / "flask_app" / "mobile_firmware_routes.py").read_text(encoding="utf-8")

    assert 'action="/admin/releases/firmware"' not in admin_template
    assert "Master and slave firmware are uploaded from each device detail page." in admin_template
    assert 'name="firmware_role" value="master"' in device_template
    assert 'name="firmware_role" value="slave"' in device_template
    assert "Master Configuration" in device_template
    assert "Slave Configuration" in device_template
    assert "Other Features" in device_template
    assert 'name="upper_sensor_source" value="master"' in device_template
    assert 'name="upper_sensor_source" value="slave"' in device_template
    assert 'name="relay_enabled"' in device_template
    assert "syncRuntimeConfigurationOptions" in device_template
    assert "Upload Master Firmware" in device_template
    assert "Upload Slave Firmware" in device_template
    assert 'name="allow_profile_mismatch" value="0"' in device_template
    assert 'name="allow_profile_mismatch" value="1" checked data-profile-mismatch-override' in device_template
    assert 'formData.set("allow_profile_mismatch",profileOverride.checked?"1":"0")' in device_template
    assert "Store for recovery/profile change" in device_template
    assert 'request.form.getlist("allow_profile_mismatch")' in server_source
    assert "profile_validation_bypassed = normalized_role == \"master\"" in server_source
    assert "expected_build_flags=None" in server_source
    assert "validate_firmware_binary_build_flags(payload" not in server_source
    assert "\"profile_validation\": \"bypassed\" if profile_validation_bypassed else \"not_applicable\"" in server_source
    assert ".release-action-row button,.release-action-row a,.release-utility-form button{min-height:34px" in admin_template
    assert "latest_global_firmware_artifact" not in server_source
    assert '"__all_customers__"' not in mobile_firmware_routes
    assert "target_role" in server_source
    assert "target_device=target_device" in mobile_firmware_routes
    assert "fetch_device_service_config=fetch_device_service_config" in server_source
    assert 'service_config.get("local_firmware_upload_enabled")' in mobile_firmware_routes
    assert "firmware_service_disabled_response" in mobile_firmware_routes
    assert '"Local firmware upload", "local_firmware_upload_enabled"' in mobile_firmware_routes
    assert '"/api/mobile/device/firmware/cloud-upgrade"' not in mobile_firmware_routes


def test_device_detail_has_device_purge_action():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def purge_device_data(device_id, remember_deleted_device=False):" in server_source
    assert 'action="/admin/customers/{{ device_id }}/delete"' in device_template
    assert 'name="delete_mode" value="purge"' not in device_template
    assert 'purge_requested = delete_mode == "purge"' not in server_source
    assert "Future check-ins are ignored until the device is registered again." in server_source
    assert "device_is_ignored(normalized_device_id)" in server_source
    assert "Purge Device Data" not in device_template
    assert "Delete device {{ device_id }} from every device-scoped database table?" in device_template
    assert "small deleted-device marker is kept" in device_template
    assert 'error = request.args.get("error", "", type=str) or None' in server_source
    assert 'success = request.args.get("success", "", type=str) or None' in server_source
    assert 'url_for(\n                "admin_customers",' in server_source
    assert '@app.route("/admin/customers/<device_id>/delete", methods=["GET", "POST"])' in server_source
    assert 'if request.method == "GET":' in server_source
    assert 'error=f"Delete for {normalized_device_id} must be submitted from the admin dashboard form."' in server_source
    assert 'logger.info("Admin device delete requested for %s", normalized_device_id)' in server_source
    assert 'logger.exception("Admin device delete failed for %s", normalized_device_id)' in server_source
    assert 'logger.exception("Admin device purge failed for %s", normalized_device_id)' not in server_source
    assert 'error=f"Delete failed for {normalized_device_id}. Check the server log for details."' in server_source
    assert 'error=f"Purge failed for {normalized_device_id}. Check the server log for details."' not in server_source
    assert 'key_identifier = quote_mysql_identifier("key")' in server_source
    assert 'f"DELETE FROM app_settings WHERE {key_identifier} LIKE ?"' in server_source


def test_device_detail_removes_cloud_firmware_upgrade():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    android_source = (
        PROJECT_ROOT.parent
        / "swt_android_app_project"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "smartwatertank"
        / "app"
        / "MainActivity.kt"
    ).read_text(encoding="utf-8")

    assert 'action="/admin/customers/{{ device_id }}/firmware/cloud-upgrade"' not in device_template
    assert "url_for('admin_device_firmware_cloud_upgrade'" not in device_template
    assert "Upgrade Master + Slave from Cloud" not in device_template
    assert "Upgrade Master from Cloud" not in device_template
    assert 'data-confirm-title="Queue cloud firmware upgrade?"' not in device_template
    assert "@app.route(\"/admin/customers/<device_id>/firmware/cloud-upgrade\", methods=[\"POST\"])" not in server_source
    assert "def admin_device_firmware_cloud_upgrade(device_id):" not in server_source
    assert "@app.route(\"/device/firmware/<int:artifact_id>/chunk\")" not in server_source
    assert "\"device_firmware_artifact_chunk\"" not in server_source
    assert "CLOUD_FIRMWARE_UPGRADE_FRESH_AFTER_SECONDS" not in server_source
    assert "OTA_BUNDLE:" not in server_source
    assert 'action="queue_cloud_firmware_upgrade"' not in server_source
    assert "startCloudFirmwareUpgrade" not in android_source
    assert "startAutomaticLocalFirmwareUpgrade()" in android_source


def test_device_detail_install_profile_template_has_deploy_fallback():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert "{% if firmware_install_profile is not defined %}" in device_template
    assert '"SWT_ARCH_ID", "value": "4"' in device_template
    assert '"SWT_DIRECT_PEER_ENABLED", "value": "0"' in device_template
    assert "Firmware Install Profile" not in device_template
    assert "{{ firmware_install_profile.description }}" not in device_template
    assert "{% for flag in firmware_install_profile.flag_rows %}" not in device_template


def test_android_app_update_check_compares_installed_version_code():
    android_source = (
        PROJECT_ROOT.parent
        / "swt_android_app_project"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "smartwatertank"
        / "app"
        / "MainActivity.kt"
    ).read_text(encoding="utf-8")

    assert '"currentVersionCode" to installedAppVersionCode().toString()' in android_source
    assert "latestVersionCode > currentVersionCode" in android_source
    assert "serverUpdateAvailable && latestVersionCode > currentVersionCode && apkUrl.isNotBlank()" in android_source


def test_android_cloud_pump_activity_chart_uses_stepped_digital_state():
    android_source = (
        PROJECT_ROOT.parent
        / "swt_android_app_project"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "smartwatertank"
        / "app"
        / "MainActivity.kt"
    ).read_text(encoding="utf-8")
    chart_view_source = (
        PROJECT_ROOT.parent
        / "swt_android_app_project"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "smartwatertank"
        / "app"
        / "DashboardChartView.kt"
    ).read_text(encoding="utf-8")

    assert "DashboardChartView.ChartStyle.TIMELINE" in android_source
    assert "R.color.cloud_chart_green" in android_source
    assert "Green lines show ON and gray lines show OFF." in android_source
    assert "Breaks indicate telemetry gaps." in android_source
    assert "section.pumpActivity.startLabel" in android_source
    assert "section.pumpActivity.endLabel" in android_source
    assert "enum class ChartStyle { LINE, BAR, STEP, TIMELINE }" in chart_view_source
    assert "private fun drawTimelineChart" in chart_view_source
    assert "private fun drawSinglePointChart" in chart_view_source
    assert "drawStepChart(canvas, left, top, width, height, min, span)" in chart_view_source
    assert "val onColor = ContextCompat.getColor(context, R.color.cloud_chart_green)" in chart_view_source
    assert "val offColor = ContextCompat.getColor(context, R.color.cloud_chart_gray)" in chart_view_source
    assert "fun lineColor(state: Int): Int = if (state == 1) onColor else offColor" in chart_view_source
    assert 'canvas.drawText("1 (ON)"' in chart_view_source
    assert 'canvas.drawText("0 (OFF)"' in chart_view_source
    assert "DashPathEffect" in chart_view_source
    assert "if (next.state == null) return@forEach" in chart_view_source
    assert "next.startTime == segment.endTime && next.state != segment.state" in chart_view_source


def test_release_versions_use_year_train_increment_syntax():
    android_build = (PROJECT_ROOT.parent / "swt_android_app_project" / "app" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    firmware_loader = (PROJECT_ROOT.parent / "swt_firmware_project" / "scripts" / "platformio_shared_device_env.py").read_text(
        encoding="utf-8"
    )
    android_release_helper = (PROJECT_ROOT / "flask_app" / "android_releases.py").read_text(encoding="utf-8")
    firmware_release_helper = (PROJECT_ROOT / "flask_app" / "firmware_artifacts.py").read_text(encoding="utf-8")

    assert 'return "${releaseVersionYearPrefix()}.$train.$patch"' in android_build
    assert 'return f"{current_version_year_prefix()}.{train}.{patch}"' in firmware_loader
    assert r"^\d{2}\.[1-9]\d*\.[1-9]\d*$" in android_release_helper
    assert rb"\b\d{2}\.[1-9]\d*\.[1-9]\d*\b".decode("ascii") in firmware_release_helper


def test_register_device_modal_is_detached_and_shows_progress():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert 'data-register-device-form' in admin_template
    assert 'data-register-device-progress' in admin_template
    assert 'setRegisterProgress' in admin_template
    assert 'document.body.appendChild(panel)' in admin_template
    assert 'panel.dataset.busy === "true"' in admin_template
    assert '.form-submit-progress-bar' in admin_template


def test_admin_dashboard_renders_online_offline_pie_chart():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert "fleet-health-chart" in admin_template
    assert "fleet-pie" in admin_template
    assert "--online-pct: {{ online_percent }}%" in admin_template
    assert "Online <b>{{ online_devices }}</b>" in admin_template
    assert "Offline <b>{{ offline_devices }}</b>" in admin_template


def test_admin_dashboard_uses_compact_aligned_layout():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert ".admin-control-room{padding:14px}" in admin_template
    assert ".hero-grid{grid-template-columns:minmax(0,1.15fr) minmax(360px,.85fr);gap:16px;align-items:start}" in admin_template
    assert ".hero-panel{padding:18px}" in admin_template
    assert ".hero-actions a,.hero-actions button{min-height:38px" in admin_template
    assert ".admin-control-room .summary-card{min-height:118px" in admin_template
    assert ".searchControls button,.searchControls a,.searchInput{min-height:38px" in admin_template
    assert ".alert-table-shell{max-height:14rem}" in admin_template
    assert ".device-table-shell{max-height:35rem}" in admin_template


def test_homepage_shows_active_identity_and_logout():
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def homepage_login_status():" in server_source
    assert "homepage_user=homepage_login_status()" in server_source
    assert "active_homepage_user = nav_auth.active_user|default(homepage_user)" in login_template
    assert "Logged in as - {{ active_homepage_user.display_name }}" in login_template
    assert '<form class="logout-form" method="post" action="/logout">' in login_template


def test_landing_page_has_compact_conversion_and_mobile_contact_content():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert "Prevent overflow, protect your motor and control your pump from anywhere." in template
    assert '>Buy Now</a>' in template
    assert 'id="buyer-confidence"' in template
    assert 'aria-label="Pricing preview"' in template
    assert "From &#8377;3,999" in template
    assert "From &#8377;9,999" in template
    assert 'class="faq-list"' in template
    assert "Does it work without Wi-Fi?" in template
    assert 'class="mobile-contact-bar"' in template
    assert "&#128222; Call" in template
    assert "&#128172; WhatsApp" in template
    assert ".comparison-table th:last-child,.comparison-table td:last-child" in template
    assert "&#10004; Yes" in template
    assert "&#10006; No" in template


def test_flask_pages_share_explanatory_term_tooltips():
    tooltip_source = (PROJECT_ROOT / "flask_app" / "static" / "js" / "global-tooltips.js").read_text(encoding="utf-8")
    pwa_head = (PROJECT_ROOT / "flask_app" / "templates" / "_pwa_head.html").read_text(encoding="utf-8")
    home_automation = (PROJECT_ROOT / "flask_app" / "templates" / "home_automation.html").read_text(encoding="utf-8")

    for term in ("telemetry", "rssi", "signal", '"municipal sensor"', "health"):
        assert term in tooltip_source
    assert "global-tooltips.js" in pwa_head
    assert "{% include '_pwa_head.html' %}" in home_automation
