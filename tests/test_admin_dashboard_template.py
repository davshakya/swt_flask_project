from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_turbidity_simulator_routes_are_registered_for_both_url_shapes():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    rules = {rule.rule for rule in server.app.url_map.iter_rules()}
    assert "/devices/<device_id>/turbidity-simulator/<role>" in rules
    assert "/admin/customers/<device_id>/turbidity-simulator/<role>" in rules


def test_admin_customer_page_renders_one_popup_status_message_slot():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert template.count('id="upload_result_panel"') == 1
    assert template.count("data-upload-result-message") == 2
    assert "data-auto-open-panel" not in template


def test_landing_page_uses_compressed_responsive_marketing_images():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'rel="preload" as="image" type="image/webp"' in template
    assert "smart-water-tank-hero-ai-1280.webp" in template
    assert template.count("<source type=\"image/webp\"") >= 6
    assert template.count('decoding="async"') >= 6
    assert template.count('width="1536" height="1024"') >= 6


def test_login_popup_inputs_use_a_visible_caret_and_selection():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    portal_input_styles = template[template.index(".portal-form input{") : template.index("textarea{", template.index(".portal-form input{"))]
    assert "caret-color:#67e8f9;" in portal_input_styles
    assert ".portal-form input::selection{" in portal_input_styles
    assert "background:#22b8cf;" in portal_input_styles


def test_login_popup_passwords_have_accessible_visibility_toggles():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'data-password-toggle="customer_password"' in template
    assert 'data-password-toggle="admin_password"' in template
    assert template.count('aria-label="Show password"') == 2
    assert 'document.querySelectorAll("[data-password-toggle]")' in template
    assert 'input.type = showPassword ? "text" : "password";' in template
    assert 'button.setAttribute("aria-label", label);' in template


def test_booking_form_requires_typed_client_side_validation():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert "const enquiryFieldRules = {" in template
    assert 'pattern="[A-Za-z][A-Za-z .\'\\-]{1,79}"' in template
    assert 'pattern="\\+?[0-9][0-9 ()\\-]{8,18}[0-9]"' in template
    assert 'id="lead_email" name="email" type="email"' in template
    assert 'autocomplete="email" maxlength="120"' in template
    assert 'id="lead_device_count" name="device_count" type="number" min="1" max="10000" step="1" inputmode="numeric"' in template
    assert 'textarea id="lead_message" name="message" maxlength="800"' in template
    assert 'What email should we send the confirmation to? Type \'skip\'' in template
    assert 'const finalNote = noteLines.join("\\n") || "Demo booking requested via SaleWell chatbot."' in template
    assert 'formData.set("return_to", "homepage")' in template
    assert "field.setCustomValidity(message)" in template
    assert "enquirySubmitButton.disabled = !isReady" in template
    assert 'class="field-warning" id="lead_phone_warning"' in template


def test_flask_static_assets_have_cache_and_compression_support():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    service_worker = (PROJECT_ROOT / "flask_app" / "static" / "service-worker.js").read_text(encoding="utf-8")
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=30)' in server_source
    assert '"Cache-Control", "public, max-age=2592000, immutable"' in server_source
    assert 'response.mimetype == "text/html"' in server_source
    assert '"no-store, no-cache, must-revalidate, max-age=0"' in server_source
    assert "water_flow_animation.html', embed='1', v='20260813-2'" in login_template
    assert "def should_gzip_response(response):" in server_source
    assert "gzip.compress(payload, compresslevel=6)" in server_source
    assert 'const CACHE_NAME = "swt-pwa-v7-native-history";' in service_worker
    assert "/static/marketing/smart-water-tank-hero-ai-1280.webp" in service_worker


def test_landing_hero_embeds_animated_dashboard_instead_of_picture_slider():
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'class="preview-dashboard-frame"' in login_template
    assert 'title="Live animated Smart Water Tank flow with complete device configuration"' in login_template
    assert 'id="dashboardPreviewFrame"' in login_template
    assert 'src="{{ url_for(\'static\', filename=\'marketing/water_flow_animation.html\', embed=\'1\'' in login_template
    assert 'src="about:blank"' not in login_template
    assert "frame.srcdoc = documentSource" not in login_template
    assert 'new ResizeObserver(fitDashboardPreview).observe(viewport)' in login_template
    assert 'id="waterPlanSlider"' not in login_template
    assert "data-plan-slide" not in login_template


def test_embedded_dashboard_allows_only_same_origin_framing():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'request.path == "/static/marketing/water_flow_animation.html"' in server_source
    assert 'response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")' in server_source
    assert 'response.headers.setdefault("X-Frame-Options", "DENY")' in server_source


def test_water_flow_animation_includes_optional_pump_assisted_source_fill():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert 'data-mode="source-pump-fill"' in animation
    assert 'id="pumpToSource"' in animation
    assert 'id="outletValve"' in animation
    assert 'id="outletValveSelector"' in animation
    assert "outlet-valve-selector source-route" not in animation
    assert "$('outletValveSelector').classList.toggle('source-route'" in animation
    assert "Municipal → Pump → Source" in animation
    assert "Source bank reached maximum 95%" in animation


def test_customer_dashboard_has_configuration_aware_live_water_visualization():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="waterVisualDialog"' in template
    assert "Visualize Water Flow" in template
    assert "openWaterVisualization,closeWaterVisualization" in template
    assert "function renderWaterVisualization" in template
    assert "waterSystemIssue" in template
    assert "municipalEnabled" in template
    assert "sourceState.monitoringActive" in template
    assert 'classList.toggle("problem",problem)' in template
    assert 'id="waterVisualAction"' in template
    assert 'id="waterComponentGrid"' in template
    assert 'id="waterIssueDetail"' in template
    assert "function normalizeWaterSystemState" in template
    assert "function evaluateWaterSystemIssues" in template
    assert '"PUMP_NO_LEVEL_CHANGE"' in template
    assert '"PUMP_FLOW_UNCONFIRMED"' in template
    assert '"FLOW_WITH_PUMP_OFF"' in template
    assert '"VALVE_POSITION_MISMATCH"' in template
    assert '"SLAVE_OFFLINE"' in template
    assert "prefers-reduced-motion:reduce" in template


def test_water_flow_animation_exposes_every_supply_plan_on_first_render():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    expected_modes = {
        "auto",
        "municipal-source",
        "municipal-upper",
        "municipal-direct",
        "pump-only",
        "source-pump-fill",
        "source-upper",
        "source-only",
        "borewell",
        "idle",
        "failsafe",
    }
    rendered_modes = {part.split('"', 1)[0] for part in animation.split('data-mode="')[1:]}

    assert rendered_modes == expected_modes


def test_homepage_how_it_works_links_to_full_water_flow_animation():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'class="button animation-page-link"' in template
    assert "url_for('static', filename='marketing/water_flow_animation.html')" in template
    assert '>See Water Flow Animation</a>' in template


def test_public_headers_share_brand_spacing_and_equal_title_text_size():
    homepage = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    pricing = (PROJECT_ROOT / "flask_app" / "templates" / "pricing.html").read_text(encoding="utf-8")

    assert ".brand-copy strong span{display:inline;margin-top:0;font-size:inherit;line-height:inherit}" in homepage
    assert ".brand-copy strong span{display:inline;margin-top:0;font-size:inherit;line-height:inherit}" in pricing
    assert "padding:9px 12px;" in homepage
    assert "padding:9px 12px;" in pricing
    assert "width:48px;\nheight:48px;\nborder-radius:16px;" in pricing


def test_setup_wizard_uses_submersible_pump_as_default_source():
    pricing = (PROJECT_ROOT / "flask_app" / "templates" / "pricing.html").read_text(encoding="utf-8")
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert 'name="water_source" value="submersible" checked' in pricing
    assert "submersible:supplies.has('submersible')" in animation
    assert "submersible?'UNDERGROUND SOURCE':'BOREWELL'" in animation


def test_pricing_page_has_whatsapp_and_working_chatbot_controls():
    pricing = (PROJECT_ROOT / "flask_app" / "templates" / "pricing.html").read_text(encoding="utf-8")

    assert 'class="pricing-whatsapp" href="https://wa.me/918796452878' in pricing
    assert 'id="pricingChatLauncher"' in pricing
    assert 'id="pricingChatPanel"' in pricing
    assert 'fetch("/chatbot/ask"' in pricing
    assert "function setPricingChatOpen(open)" in pricing


def test_water_flow_animation_labels_plan_and_aligns_connection_indicators():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert '<span class="diagram-kicker">Customer route demonstration</span>' in animation
    assert 'id="connectionLegend"' in animation
    assert 'translate(35 770)' in animation
    assert 'translate(870 770)' in animation
    assert animation.count('class="small compact"') >= 5


def test_water_flow_animation_indicator_names_auto_and_manual_plans():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert "?'● AUTO TOUR · '+plan.toUpperCase()" in animation
    assert ":'● SELECTED DEMO · '+plan.toUpperCase();" in animation
    assert "● MANUAL VIEW" not in animation


def test_water_flow_animation_removes_optional_municipal_hardware_from_source_only_demo():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert "body.source-only #outletValve" in animation
    assert "body.source-only #threeWayValve" in animation
    assert "body.source-only #municipalSensorCard" in animation
    assert "body.source-only #valveMetricCard" in animation
    assert "body.source-only #valveSensorCard" in animation
    assert "No municipal inlet or motorized valves are installed" in animation
    assert "independent OPEN / CLOSE pair for each installed valve" in animation


def test_water_flow_animation_reconciles_hardware_and_wires_with_device_setup():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert "function applyDeviceSetup(m,sensorInstalled)" in animation
    assert "setInstalled($('municipalSensor'),hasMunicipal&&sensorInstalled)" in animation
    assert "setInstalled($('municipalWire'),hasMunicipal&&sensorInstalled)" in animation
    assert "setInstalled($('sourceTankHardware'),hasSourceTank)" in animation
    assert "setInstalled($('sourceLevelControllerWire'),hasSourceTank)" in animation
    assert "setInstalled($('threeWayValve'),hasValves)" in animation
    assert "setInstalled($('outletValveControlWire'),hasValves)" in animation
    assert "const hasWaterQuality=devices?devices.waterQuality===true:true" in animation
    assert "setInstalled($('sourceQualityHardware'),hasSourceTank&&hasWaterQuality)" in animation
    assert "document.body.classList.toggle('no-water-quality',!hasWaterQuality)" in animation
    assert "const hasCheckValve=hasMunicipal&&hasSourceTank" in animation


def test_water_flow_animation_can_really_pause_and_respects_reduced_motion():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert "prefers-reduced-motion:reduce" in animation
    assert "!$('tankSim').checked" in animation


def test_pricing_and_animation_share_plan_configuration_catalog():
    import json

    pricing = (PROJECT_ROOT / "flask_app" / "templates" / "pricing.html").read_text(encoding="utf-8")
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")
    catalog_path = PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_plan_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    assert "addPlanCustomizeLinks" in pricing
    assert 'link.href = "#find-my-plan"' in pricing
    assert 'link.textContent = "Customize Your Setup"' in pricing
    assert 'separator.textContent = "OR"' in pricing
    assert "js-view-animation" not in pricing
    assert 'url.searchParams.set("plan", planName)' in pricing
    assert 'id="animationModal"' in pricing
    assert 'id="planAnimationFrame"' in pricing
    assert 'class="animation-modal-title"' in pricing
    assert 'url.searchParams.set("embed", "1")' in pricing
    assert 'id="wizardAnimation"' in pricing
    assert "updateWizardAnimationButton(plan, {upperTanks:upperTankCount" in pricing
    assert "openAnimationModal(planName, url.toString())" in pricing
    assert 'url.searchParams.set("configured", "1")' in pricing
    assert 'url.searchParams.set("upperTanks"' in pricing
    assert 'url.searchParams.set("sourceTanks"' in pricing
    assert 'url.searchParams.set("supplies"' in pricing
    assert "openAnimationModal(planName, url.toString())" in pricing
    assert 'animationFrame.src = "about:blank"' in pricing
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in pricing
    assert 'grid-template-areas:"summary price"' in pricing
    assert ".plan-card>.feature-list{grid-area:features}" in pricing
    assert "function initializePlanCarousel()" in pricing
    assert 'className = "plans-carousel"' in pricing
    assert 'class="plans-carousel-button is-prev"' in pricing
    assert 'class="plans-carousel-button is-next"' in pricing
    assert "planCards.forEach((card, index) =>" in pricing
    assert "track.appendChild(card)" in pricing
    assert "height:clamp(520px,58vh,610px)" in pricing
    assert ".plans-carousel-track .plan-action span{display:none}" in pricing
    assert "align-content:start;align-self:start" in pricing
    assert "transform:none!important" in pricing
    assert ".plans-carousel-track .price-note{display:none!important}" in pricing
    assert 'button.className = "plan-info-button"' in pricing
    assert 'panel.className = "plan-info-panel"' in pricing
    assert 'button.setAttribute("aria-expanded", "false")' in pricing
    assert 'More information about ${planName}' in pricing
    assert "loadSelectedPlan" in animation
    assert "const params=new URLSearchParams(location.search),requested=params.get('plan')" in animation
    assert "planSetup.modes" in animation
    assert "params.get('configured')==='1'" in animation
    assert "get('embed')==='1'" in animation
    assert "body.embedded .diagram-header,body.embedded .sidebar{display:none!important}" in animation
    assert "aspect-ratio:1050/790" in pricing
    assert "configuredModes.push('municipal-source','municipal-upper','source-pump-fill','source-upper')" in animation
    assert "configuredModes.push('municipal-direct')" in animation
    assert "configuredModes.push('borewell')" in animation
    assert "configuredModes.push(hasSourceTank?'source-only':'pump-only')" in animation
    assert "sourceTank:hasSourceTank" in animation
    assert catalog["plans"]["Enterprise Modular"]["animation"] is False
    assert all(catalog["plans"][name]["animation"] for name in (
        "Home Basic", "Home Control", "Home Cloud Pro", "RWA Standard", "Commercial AI Pro"
    ))
    for name in ("Home Basic", "Home Control", "Home Cloud Pro", "Dealer / Installer Kit"):
        assert catalog["plans"][name]["initialMode"] == "borewell"
        assert catalog["plans"][name]["devices"]["submersible"] is True
        assert catalog["plans"][name]["devices"]["sourceTank"] is False
        assert catalog["plans"][name]["devices"]["municipal"] is False
        assert catalog["plans"][name]["devices"]["motorizedValves"] is False
    for name in ("RWA Standard", "Commercial AI Pro"):
        assert catalog["plans"][name]["devices"]["sourceTank"] is True
        assert catalog["plans"][name]["devices"]["municipal"] is False
        assert catalog["plans"][name]["devices"]["motorizedValves"] is False


def test_water_flow_animation_includes_direct_municipal_upper_without_source_or_valves():
    animation = (PROJECT_ROOT / "flask_app" / "static" / "marketing" / "water_flow_animation.html").read_text(encoding="utf-8")

    assert 'data-mode="municipal-direct"' in animation
    assert 'id="municipalOnlyBypass"' in animation
    assert "body.municipal-direct #sourceTankHardware" in animation
    assert "body.municipal-direct #threeWayValve" in animation
    assert "body.municipal-direct #outletValve" in animation
    assert "Municipal → Pump → Upper · No source / valves" in animation
    assert "Direct municipal setup requires a healthy water-availability input" in animation


def test_homepage_replaces_picture_slider_with_animated_dashboard():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'class="preview-dashboard-frame"' in template
    assert "marketing/water_flow_animation.html" in template
    assert 'id="waterPlanSlider"' not in template
    assert "marketing/water-plan-slides/" not in template
    assert '<figure class="preview-slide' not in template
    assert '<button class="preview-slider-dot' not in template


def test_sales_enquiry_server_validation_matches_booking_form_rules():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'valid_segments = {' in server_source
    assert 're.fullmatch(r"[A-Za-z][A-Za-z .\'-]*", cleaned["name"])' in server_source
    assert 'len(cleaned["email"]) > 120' in server_source
    assert 're.fullmatch(r"\\+?[0-9][0-9 ()-]*[0-9]", cleaned["phone"])' in server_source
    assert 'len(phone_digits) > 15' in server_source
    assert 'cleaned["segment"] not in valid_segments' in server_source
    assert 'visitor_note = cleaned["message"] or "Demo booking requested;' in server_source
    assert 'cleaned["source_configuration"] not in valid_source_configurations' in server_source
    assert 'cleaned["pump_type"] not in valid_pump_types' in server_source
    assert 'Water configuration:' in server_source
    validator = server_source[server_source.index("def validate_sales_enquiry_payload(form):"):server_source.index("def build_sales_enquiry_email", server_source.index("def validate_sales_enquiry_payload(form):"))]
    for allowed_values in (
        "valid_upper_layouts = {",
        "valid_source_configurations = {",
        "valid_pump_types = {",
        "valid_maintenance_preferences = {",
    ):
        assert allowed_values in validator

    for template_name in ("login.html", "pricing.html"):
        template = (PROJECT_ROOT / "flask_app" / "templates" / template_name).read_text(encoding="utf-8")
        assert 'name="upper_tank_count"' not in template
        assert 'name="source_tank_count"' not in template
        assert 'id="lead_upper_layout"' not in template
        assert 'id="lead_source_configuration"' not in template
        assert 'id="lead_pump_type"' not in template
        assert 'id="lead_maintenance_preference"' not in template
        assert "Customize Your Water Setup" not in template
    pricing_template = (PROJECT_ROOT / "flask_app" / "templates" / "pricing.html").read_text(encoding="utf-8")
    assert "Customize your water setup." in pricing_template
    assert "Customized water setup:" in pricing_template


def test_demo_booking_uses_waf_safe_canonical_route():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    route_block = server_source[server_source.index('@app.route("/sales/enquiry"'):server_source.index("def sales_enquiry():")]
    assert '@app.route("/sales/enquiry", methods=["GET", "POST"])' in route_block
    assert '@app.route("/sales/enquiry/", methods=["GET", "POST"])' in route_block
    # Flask applies decorators from the bottom up, so the closest route is the
    # first registered rule and therefore the URL emitted by url_for().
    assert route_block.rstrip().endswith('@csrf_protect')
    assert route_block.index('@app.route("/book-demo"') > route_block.index('@app.route("/sales/enquiry/"')

    for template_name in ("login.html", "pricing.html"):
        template = (PROJECT_ROOT / "flask_app" / "templates" / template_name).read_text(encoding="utf-8")
        assert 'action="{{ url_for(\'sales_enquiry\') }}"' in template
        email_tag = template.split('id="lead_email"', 1)[1].split(">", 1)[0]
        assert " required" not in email_tag
        assert 'name="message" maxlength="800"' in template
        assert 'name="message" minlength="10"' not in template


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
    assert "upper_has_live_input = admin_sensor_reachable(upper_sensor)" in server_source
    assert "and upper_has_live_input" in server_source
    assert '"direct_peer_last_packet_age_s": payload.get("direct_peer_last_packet_age_s")' in server_source
    assert 'cleaned.get("direct_peer_last_packet_age_s")' in server_source
    assert '"direct_peer_last_packet_age_s": "INTEGER"' in server_source


def test_admin_upper_sensor_requires_sensor_health_even_when_slave_packet_is_fresh():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    fields = server.admin_relay_sensor_status_fields(
        {
            "telemetry_status": "live",
            "direct_peer_last_packet_age_s": 1,
            "level": 42.0,
            "upper_sensor": "ERROR",
        },
        {
            "main_sensor_enabled": True,
            "slave_device_enabled": True,
            "slave_upper_sensor_enabled": True,
        },
    )

    assert fields["upper_sensor_status_label"] == "Unreachable"
    assert fields["upper_sensor_status_tone"] == "offline"


def test_admin_upper_sensor_rejects_cached_simulator_level_after_simulator_is_off():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    fields = server.admin_relay_sensor_status_fields(
        {
            "telemetry_status": "live",
            "direct_peer_last_packet_age_s": 1,
            "level": 0.0,
            "upper_sensor": "OK",
            "upper_data_fresh": True,
            "upper_tank_simulator": "OFF",
            "upper_sensor_pulse_us": 0,
        },
        {
            "main_sensor_enabled": True,
            "slave_device_enabled": True,
            "slave_upper_sensor_enabled": True,
        },
    )

    assert fields["upper_sensor_status_label"] == "Unreachable"
    assert fields["upper_sensor_status_tone"] == "offline"


def test_invalid_firmware_level_remains_unavailable_in_enriched_snapshot():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    snapshot = server.enrich_snapshot(
        {
            "level": -1.0,
            "sensor": "ERROR",
            "created_at": server.now_utc().strftime(server.TIMESTAMP_FORMAT),
            "device_id": "swt-test-invalid-upper",
        }
    )

    assert snapshot["level"] is None
    assert snapshot["level_valid"] is False


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
    assert 'window.location.assign(targetUrl.href)' in smooth_navigation
    assert 'window.location.replace(targetUrl.href)' in smooth_navigation
    assert 'history.scrollRestoration = "auto"' in smooth_navigation
    assert 'history.pushState' not in smooth_navigation
    assert 'window.addEventListener("popstate"' not in smooth_navigation
    assert 'document.addEventListener("click"' not in smooth_navigation
    assert 'document.addEventListener("submit"' not in smooth_navigation
    assert 'cache: "no-store"' not in smooth_navigation
    assert "20260812-native-history-v1" in pwa_head
    assert '"/static/js/smooth-navigation.js"' in service_worker
    assert 'const CACHE_NAME = "swt-pwa-v7-native-history";' in service_worker


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
    assert 'const ANALYTICS_CACHE_SCHEMA_VERSION="v16-usage-validity";' in dashboard_template
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
    assert "const spinning=running||pendingActive" in customer_template
    assert "if(levelIncreasing&&!isPumpRunning(motor))return\"START\";" not in customer_template
    assert "state.pendingPumpStartUntil=startRequest?Date.now()+PUMP_START_GRACE_MS:0" in customer_template


def test_customer_dashboard_stop_button_uses_effective_running_state():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    update_start = customer_template.index("function updateCommandAvailability")
    update_body = customer_template[update_start : customer_template.index("function renderCustomerOverview", update_start)]

    assert "const pendingStartActive=state.pendingPumpStart&&Date.now()<state.pendingPumpStartUntil;" in update_body
    assert "const running=isPumpRunning(snapshot?.motor)||pendingStartActive||state.upperTankIncreasing;" in update_body
    assert 'nodesById("btn_off").forEach((button)=>{button.disabled=!baseEnabled||!running;});' in update_body


def test_customer_dashboard_pump_activity_shows_metrics_without_graph():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="motorChart"' not in customer_template
    assert '["motor","motorChart","Pump state"]' not in customer_template
    assert 'id="chartPumpRuntime"' in customer_template
    assert 'id="chartPumpStarts"' in customer_template
    assert 'id="chartPumpAverage"' in customer_template
    assert 'id="chartPumpDuty"' in customer_template
    assert "Hourly water use" in customer_template
    assert "data.pattern?.time||[]" in customer_template
    assert 'xScale:"time"' in customer_template
    assert customer_template.count('class="chart-scroll"') == 3
    assert 'aria-label="Scrollable tank level timeline"' in customer_template
    assert 'aria-label="Scrollable daily water use timeline"' in customer_template
    assert 'aria-label="Scrollable hourly water use timeline"' in customer_template
    assert 'aria-label="Scrollable pump activity timeline"' not in customer_template
    assert "function prepareScrollableChart(" in customer_template
    assert 'Math.min(8000,Math.max(viewportWidth,96+(count*pointWidth)))' in customer_template
    assert 'canvas.style.setProperty("--chart-width"' in customer_template
    assert "function scrollChartToLatest(canvas)" in customer_template
    assert "viewport.scrollWidth-viewport.clientWidth" in customer_template
    assert "[charts.level.canvas,charts.daily.canvas,charts.pattern.canvas].forEach(scrollChartToLatest);" in customer_template
    assert ".chart-scroll{width:100%;overflow-x:auto" in customer_template
    assert "function formatDurationSeconds(value)" in customer_template
    assert "pump.avg_run_seconds" in customer_template
    assert 'ctx.lineTo(endX,stateY(next.state));' in customer_template
    assert 'const tickCount=Math.max(2,Math.min(5,Math.round(box.plotWidth/170)));' in customer_template
    assert 'function isCustomerWaterEvent(event)' in customer_template
    assert 'Hidden because sensor changes exceed the water supported by observed refill cycles.' in customer_template
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
    assert "executeMotorCommand(request.path,request.label,request.durationMinutes)" in customer_template
    assert 'id="dashboardSettings"' in customer_template
    assert '<details id="dashboardSettings"' not in customer_template
    assert '<div id="dashboardSettings" class="customer-settings-pane">' in customer_template
    assert customer_template.index('id="dashboardSettings"') < customer_template.index('id="eventTimeline"')
    assert 'class="panel customer-only customer-tools-panel"' in customer_template
    assert 'class="customer-tools-grid"' in customer_template
    assert 'class="customer-guidance-panel"' in customer_template
    assert '.customer-dashboard .panel.customer-tools-panel{padding:0;overflow:hidden}' in customer_template
    assert '.customer-dashboard .customer-tools-grid{display:grid;grid-template-columns:minmax(320px,.78fr) minmax(0,1.22fr);align-items:stretch}' in customer_template
    assert 'class="settings customer-settings-grid"' in customer_template
    assert '.customer-dashboard .customer-settings-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:12px}' in customer_template
    assert '<details class="settings-disclosure" style="margin-top:12px"><summary>Technical details</summary>' not in customer_template
    assert '<div class="customer-technical-details"><h4>Technical details</h4>' in customer_template
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
    assert 'analytics?.daily?.reliable!==true' in customer_template
    assert 'Savings insight appears after two complete days of reliable usage history.' in customer_template
    assert 'value:"0.0 L"' not in customer_template
    assert '`+${increaseLiters.toFixed(1)} L used`' in customer_template
    assert 'const ANALYTICS_CACHE_SCHEMA_VERSION="v16-usage-validity";' in customer_template
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
    assert 'id="customerKpiUsage"' in customer_template
    assert 'id="ai_tomorrow_usage"' in customer_template
    assert 'id="consumption_rate"' in customer_template
    assert 'id="customerSavingsNow"' in customer_template
    assert 'class="customer-stat-grid customer-usage-grid"' in customer_template
    assert '.customer-dashboard .customer-usage-grid{grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:14px}' in customer_template
    assert '.customer-dashboard .customer-usage-grid .card{min-width:0;min-height:112px;padding:14px 16px;border-radius:18px}' in customer_template
    assert '.customer-dashboard .customer-usage-grid .label,.customer-dashboard .customer-usage-grid .value,.customer-dashboard .customer-usage-grid .subvalue' in customer_template
    assert 'id="customerPumpRuntime"' not in customer_template
    assert 'id="customerPumpStarts"' not in customer_template
    assert 'id="customerPumpDuty"' not in customer_template
    assert 'id="customerPumpStopThreshold"' not in customer_template
    assert 'class="analytics-toolbar"' in customer_template
    assert 'class="panel tank-chart-panel"' in customer_template
    assert 'id="chartLevelHigh"' in customer_template
    assert 'id="chartPumpAverage"' in customer_template
    assert 'Building baseline' in customer_template
    assert 'id="customerFirmware"' not in customer_template
    assert 'id="customerUptime"' in customer_template
    assert 'id="customerSignal"' in customer_template
    assert 'id="customerMemory"' in customer_template
    assert 'snapshot.firmware_version||"Not reported"' in customer_template
    assert '<span>Tank status</span>' not in customer_template
    assert 'id="customerKpiTankStatus"' not in customer_template
    assert '<span>Remaining water</span>' in customer_template
    assert 'id="customerKpiRemainingWater"' in customer_template
    assert 'id="customerSuggestedAction"' in customer_template
    assert customer_template.count('class="customer-hero-stat customer-insight-card"') == 4
    assert customer_template.index('<span class="label">Pump Control</span>') < customer_template.index('id="customerSuggestedAction"')
    assert '.customer-dashboard .customer-insight-kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:20px}' in customer_template
    assert '.customer-dashboard .customer-insight-card{display:grid;grid-template-rows:auto 1fr;' in customer_template
    assert '.customer-dashboard .customer-insight-card span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' in customer_template
    assert '.customer-dashboard .customer-insight-card strong{align-self:end;' in customer_template
    assert 'id="headerLastSync"' in customer_template
    assert 'id="customerPumpLastStarted"' not in customer_template
    assert 'id="customerPumpLastStopped"' not in customer_template
    assert 'id="chartLevelCurrent"' in customer_template
    assert 'function analyticsReadinessText(' in customer_template
    assert 'Need ${remaining} more complete day' in customer_template
    assert 'No alerts today. No customer-relevant device activity has been recorded yet.' in customer_template
    assert 'changeRaw!==null&&changeRaw!==undefined' in customer_template
    assert 'class="panel pump-chart-panel"' in customer_template
    assert 'const EVENT_FEED_FETCH_LIMIT=30;' in customer_template
    assert 'function customerEventMessage(event)' in customer_template
    assert 'function customerDerivedTimelineEvents()' in customer_template
    assert '"pump_started","pump_stopped","pump_no_level_rise","mode_changed"' in customer_template
    assert 'VIEWER_ROLE==="customer"?customerEventMessage(event)' in customer_template
    assert '(state.data.events||[]).filter(isCustomerWaterEvent)' in customer_template
    assert 'id="pumpAnalyticsSourceNote"' not in customer_template
    assert 'class="pump-facts"' not in customer_template
    assert 'const customerMetricNumber=(value)=>VIEWER_ROLE==="customer"?Math.abs(Number(value)):Number(value);' in customer_template
    assert 'id="customerMonthlyEstimate"' in customer_template
    assert 'id="assistantTankRange"' not in customer_template


def test_customer_ai_analysis_tracks_selected_range_and_defaults_to_seven_days():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert "const DEFAULT_ANALYTICS_RANGE_DAYS=7;" in customer_template
    assert 'quickRange:DEFAULT_ANALYTICS_RANGE_DAYS' in customer_template
    assert 'id="range_7" class="btn-lite active"' in customer_template
    assert "function updateAnalyticsRangeContext(" in customer_template
    assert "function beginAnalyticsRangeChange()" in customer_template
    assert 'updateCustomerSpotlightCards({snapshot:state.data.snapshot,system:state.data.systemStatus,analytics:null})' in customer_template
    assert 'const requestQuery=query();' in customer_template
    assert 'if(state.inFlight.analytics){if(state.analyticsRequestQuery===requestQuery)return;state.controllers.analytics?.abort();}' in customer_template
    assert 'const READY_ANALYTICS_RANGE_DAYS=[1,7];' in customer_template
    assert 'id="range_30"' not in customer_template
    assert 'function quickRangeQuery(days)' in customer_template
    assert 'async function prefetchReadyAnalyticsRanges()' in customer_template
    assert 'function scheduleReadyAnalyticsPrefetch()' in customer_template
    assert 'saveAnalyticsCache(data,requestQuery,{remember:false})' in customer_template
    assert 'function analyticsCacheKey(queryString=query())' in customer_template
    assert 'function loadAnalyticsCache(queryString=query())' in customer_template
    assert 'scheduleReadyAnalyticsPrefetch();' in customer_template
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
    assert 'data-demo-title="Dashboard Without Municipal Feature"' in login_template
    assert "No municipal sensor or motorized valve required." in login_template
    assert 'data-total-ms="33000"' in login_template


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
    assert 'window.swtSyncLowerSensorSetupVisibility&&window.swtSyncLowerSensorSetupVisibility();window.swtSyncRuntimeConfigurationOptions&&window.swtSyncRuntimeConfigurationOptions()' in device_template
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


def test_android_cloud_pump_activity_shows_metrics_and_scrollable_chart():
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
    android_layout = (
        PROJECT_ROOT.parent
        / "swt_android_app_project"
        / "app"
        / "src"
        / "main"
        / "res"
        / "layout"
        / "activity_main.xml"
    ).read_text(encoding="utf-8")

    assert "cloudPumpActivityChart" not in android_layout
    assert "cloudPumpActivityScroll" not in android_layout
    assert "cloudHourlyPatternChart" not in android_layout
    assert "cloudHourlyPatternCard" not in android_layout
    for metric_id in ("cloudPumpRuntimeValue", "cloudPumpStartsValue", "cloudPumpAverageValue", "cloudPumpDutyValue"):
        assert metric_id in android_layout
    assert "binding.cloudPumpRuntimeValue" in android_source
    assert "binding.cloudPumpStartsValue" in android_source
    assert "binding.cloudPumpAverageValue" in android_source
    assert "binding.cloudPumpDutyValue" in android_source
    assert "binding.cloudPumpActivityChart.setChart" not in android_source


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
    assert ".device-table-shell .admin-table{width:100%;min-width:0;table-layout:fixed}" in admin_template
    assert ".device-horizontal-scroll{display:none}" in admin_template
    assert ".searchControls{grid-template-columns:minmax(320px,1fr) auto auto auto" in admin_template
    assert "text-overflow:clip;white-space:normal;overflow-wrap:anywhere;text-align:left" in admin_template
    assert ".device-table-shell .cell-main,.device-table-shell .cell-sub,.device-table-shell .device-link" in admin_template
    assert ".device-table-shell .admin-table th:first-child,.device-table-shell .admin-table td:first-child{padding-left:12px}" in admin_template
    assert ".device-table-shell .admin-table th:nth-child(9),.device-table-shell .admin-table td:nth-child(9){text-align:left;vertical-align:middle}" in admin_template


def test_homepage_shows_active_identity_and_logout():
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def homepage_login_status():" in server_source
    assert "homepage_user=homepage_login_status()" in server_source
    assert "active_homepage_user = nav_auth.active_user|default(homepage_user)" in login_template
    assert "Logged in as - {{ active_homepage_user.display_name }}" not in login_template
    assert "<strong>{{ active_homepage_user.display_name }}</strong>" in login_template
    assert 'class="login-account-panel"' in login_template
    assert 'aria-controls="loginModal"' in login_template
    assert '<form class="logout-form" method="post" action="/logout">' in login_template


def test_homepage_template_has_valid_jinja_syntax():
    from jinja2 import Environment

    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    Environment().parse(template)


def test_landing_page_has_compact_conversion_and_mobile_contact_content():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert "Prevent overflow, protect your motor and control your pump from anywhere." in template
    assert '>Buy Now</a>' in template
    assert 'id="buyer-confidence"' in template
    assert 'aria-label="Pricing preview"' in template
    assert "From &#8377;4,999" in template
    assert "From &#8377;10,999" in template
    assert 'class="faq-list"' in template
    assert "Does it work without Wi-Fi?" in template
    assert 'class="mobile-contact-bar"' in template
    assert 'aria-label="Call SaleWell"' in template
    assert 'aria-label="Contact SaleWell on WhatsApp"' in template
    assert 'class="contact-icon whatsapp-icon"' in template
    assert 'aria-label="Book a free demo"' in template
    assert 'aria-label="Open chat"' in template
    assert 'body.chatbot-open .mobile-contact-bar{display:none}' in template
    assert 'document.body.classList.toggle("chatbot-open", isOpen);' in template
    assert '.mobile-contact-bar a span,.mobile-contact-bar button span' in template
    assert 'height:38px;min-height:38px;max-height:38px' in template
    assert 'class="contact-tab" href="#enquiry"' in template
    assert 'class="button is-primary-cta" href="#enquiry" aria-label="Book a free demo"' not in template
    assert '.mobile-contact-bar .contact-tab:visited' in template
    assert '-webkit-text-fill-color:#fff' in template
    assert 'Date.now() - chatbotOpenedAt < 700' in template
    assert 'mobileChatbotButton.addEventListener("click", (event) =>' in template
    assert 'event.stopPropagation();' in template
    assert 'id="mobileChatbotButton"' in template
    assert "--chat-text:#f1fbfd" in template
    assert "--chat-control-text:#dff8fc" in template
    assert "font-size:14px;\nline-height:1.48" in template
    assert 'setChatbotOpen(true);' in template
    assert "grid-template-columns:repeat(4,minmax(0,1fr))" in template
    assert ".enquiry-modal{z-index:120}" in template
    assert ".enquiry-modal .form-actions{" in template
    assert ".comparison-table th:last-child,.comparison-table td:last-child" in template
    assert "&#10004; Yes" in template
    assert "&#10006; No" in template


def test_sales_content_uses_current_complete_wireless_architecture():
    homepage = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    pricing = (PROJECT_ROOT / "flask_app" / "templates" / "pricing.html").read_text(encoding="utf-8")

    assert "Wireless Tank Sensor Node" in homepage
    assert "Complete Wireless Smart Tank Kit" in homepage
    assert "Single Controller Kit" not in homepage
    assert "Shielded Wire or Dual Node" not in homepage
    assert "One wireless setup for every building height." not in pricing
    assert 'id="modules"' not in pricing
    assert "Every base plan includes a complete wireless tank setup and automatic pump control." in pricing
    assert "Included Equipment" in pricing
    assert "one-time equipment price" in homepage
    assert "Phone app and live screen not included" in pricing
    assert "&#8377;5,999" in pricing
    assert "Phone access at the property only. Remote access and AI are not included." in pricing
    assert "Home Basic and Home Control connect directly" in pricing
    assert "<th>Home Wi-Fi / Internet</th>" in pricing
    assert "AI analytics and insights" in pricing
    assert "Local mobile app and live monitoring" in pricing
    assert "+ &#8377;1,000 one-time" in pricing
    assert "Wireless range extension node" in pricing
    assert pricing.count('class="addon-fit"') == 10
    assert 'id="planWizard"' in pricing
    assert 'id="wizardPlan"' in pricing
    assert 'id="wizardPrice"' in pricing
    assert '"Home Basic": 4999' in pricing
    assert '"Home Control": 5999' in pricing
    assert '"Home Cloud Pro": 8499' in pricing
    assert '"RWA Standard": 10999' in pricing
    assert '"Commercial AI Pro": 13999' in pricing
    assert "additionalUpperMcuCount * 2500" in pricing
    assert 'upperLayout === "shared" ? 1 : upperTankCount' in pricing
    assert 'property === "managed" || requiredSensorNodeCount >= 3' in pricing
    assert "sourceSensorCount * 2000" in pricing
    assert "estimatedPrice += 7999" in pricing
    assert "Municipal Water Kit (2 motorized valves + 1 water sensor)" in pricing
    assert pricing.count("Installation and plumbing charged separately by work type") == 7
    assert 'name="pump"' not in pricing
    assert "Every device plan includes automatic pump control" in pricing
    assert "source-tank sensor" in pricing
    assert '" + site quote"' in pricing
    assert 'name="maintenance" value="monthly"' in pricing
    assert 'name="maintenance" value="annual"' in pricing
    assert "Optional monthly or annual maintenance" in pricing
    assert 'id="use-cases"' in pricing
    assert 'id="installation"' in pricing
    assert 'id="faqs"' in pricing
    assert 'class="mobile-contact-bar"' in pricing
    assert 'aria-label="Call SaleWell"' in pricing
    assert 'aria-label="Contact SaleWell on WhatsApp"' in pricing
    assert 'aria-label="Book a free demo"' in pricing
    assert 'aria-label="Open chat"' in pricing
    assert "grid-template-columns:repeat(4,minmax(0,1fr))" in pricing
    assert "Built for homes and managed properties." not in pricing
    assert "A simple path from basics to AI analytics." not in pricing
    assert "Warranty coverage varies by kit and project scope." in pricing
    assert "Single Controller Kit" not in pricing
    assert "Shielded Wire or Dual Node" not in pricing


def test_flask_pages_share_explanatory_term_tooltips():
    tooltip_source = (PROJECT_ROOT / "flask_app" / "static" / "js" / "global-tooltips.js").read_text(encoding="utf-8")
    pwa_head = (PROJECT_ROOT / "flask_app" / "templates" / "_pwa_head.html").read_text(encoding="utf-8")

    for term in ("telemetry", "rssi", "signal", '"municipal sensor"', "health"):
        assert term in tooltip_source
    assert "global-tooltips.js" in pwa_head


def test_customer_remaining_time_uses_live_snapshot_while_analytics_loads():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert "function estimatedRemainingHours(snapshot,analytics)" in customer_template
    assert "return remaining/liveRate" in customer_template
    assert "const remainingHours=estimatedRemainingHours(snapshot,analytics);" in customer_template


def test_combined_fill_status_uses_firmware_active_fill_flags():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert "const sourcePumpActive=flagEnabled(snapshot.source_pump_fill_active,false);" in customer_template
    assert "const sourceGravityActive=flagEnabled(snapshot.source_gravity_fill_active,false);" in customer_template
    assert "const filling=sourceGravityActive||pumpActive||valveFlowActive;" in customer_template


def test_event_log_explains_motorized_valve_state_and_water_path():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'unknown:"Position not reported"' in customer_template
    assert 'closed:"Closed (source-tank inlet selected)"' in customer_template
    assert 'source:"Source Tank → Upper Tank"' in customer_template
    assert "Water path: ${valveRoute}" in customer_template


def test_today_savings_card_does_not_request_impossible_complete_days():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'if(analyticsRangeIsToday())return{' in customer_template
    assert 'value:"Select 7 Days"' in customer_template
    assert "Savings compares complete days." in customer_template


def test_all_today_usage_cards_use_today_specific_readiness_text():
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'if(analyticsRangeIsToday())return"Collecting today"' in customer_template
    assert "Today is still in progress; daily averages appear in the 7 Days view." in customer_template
    assert "Today is still in progress; use the 7 Days view for a complete-day forecast." in customer_template
    assert "Waiting for complete days" not in customer_template


def test_login_modal_has_scoped_high_contrast_theme():
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'id="swt-login-modal-contrast-fix"' in login_template
    assert "#loginModal .modal-panel" in login_template
    assert "background:#102f39!important" in login_template
    assert "-webkit-text-fill-color:#f4fcfd!important" in login_template
    assert '#loginModal .portal-form input:not([type="hidden"]):-webkit-autofill' in login_template
    assert "-webkit-box-shadow:0 0 0 1000px #102f39 inset!important" in login_template
