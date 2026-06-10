const state = {
  channels: [],
  fanSpeed: 0,
  devices: readRegisteredDevices(),
};

const els = {
  shell: document.querySelector(".shell"),
  mode: document.getElementById("mode"),
  deviceId: document.getElementById("deviceId"),
  registeredDeviceSelect: document.getElementById("registeredDeviceSelect"),
  boardTableBody: document.getElementById("boardTableBody"),
  boardTableMeta: document.getElementById("boardTableMeta"),
  boardVisiblePill: document.getElementById("boardVisiblePill"),
  boardUpdatedPill: document.getElementById("boardUpdatedPill"),
  boardSearch: document.getElementById("boardSearch"),
  boardFilter: document.getElementById("boardFilter"),
  connectionStatus: document.getElementById("connectionStatus"),
  activeBoardName: document.getElementById("activeBoardName"),
  activeBoardState: document.getElementById("activeBoardState"),
  selectedBoardTitle: document.getElementById("selectedBoardTitle"),
  totalBoardsKpi: document.getElementById("totalBoardsKpi"),
  onlineBoardsKpi: document.getElementById("onlineBoardsKpi"),
  offlineBoardsKpi: document.getElementById("offlineBoardsKpi"),
  activeCustomersKpi: document.getElementById("activeCustomersKpi"),
  alertsKpi: document.getElementById("alertsKpi"),
  onlineStatusText: document.getElementById("onlineStatusText"),
  offlineStatusText: document.getElementById("offlineStatusText"),
  lowSignalText: document.getElementById("lowSignalText"),
  alertList: document.getElementById("alertList"),
  activityTimeline: document.getElementById("activityTimeline"),
  signalBar: document.getElementById("signalBar"),
  signalCaption: document.getElementById("signalCaption"),
  onlineBar: document.getElementById("onlineBar"),
  onlineCaption: document.getElementById("onlineCaption"),
  modeLabel: document.getElementById("modeLabel"),
  ipLabel: document.getElementById("ipLabel"),
  rssiLabel: document.getElementById("rssiLabel"),
  fanLabel: document.getElementById("fanLabel"),
  channels: document.getElementById("channels"),
  fanSpeed: document.getElementById("fanSpeed"),
  fanSpeedText: document.getElementById("fanSpeedText"),
  refreshButton: document.getElementById("refreshButton"),
  allOnButton: document.getElementById("allOnButton"),
  allOffButton: document.getElementById("allOffButton"),
};

function readRegisteredDevices() {
  const script = document.getElementById("homeAutomationDevices");
  if (!script) {
    return [];
  }
  try {
    const devices = JSON.parse(script.textContent || "[]");
    return Array.isArray(devices) ? devices : [];
  } catch (error) {
    return [];
  }
}

function isCloudMode() {
  return els.mode.value === "cloud";
}

function apiPath(path) {
  if (isCloudMode()) {
    const deviceId = encodeURIComponent(selectedDeviceId());
    return `/api/home-automation/cloud/${deviceId}/${path}`;
  }
  if (els.shell.dataset.localProxy === "true") {
    return `/api/home-automation/local/${path}`;
  }
  return `${normalizedLocalUrl()}/${path}`;
}

function cloudApiPath(deviceId, path) {
  return `/api/home-automation/cloud/${encodeURIComponent(deviceId)}/${path}`;
}

function normalizedLocalUrl() {
  const rawValue = document.getElementById("localUrl").value.trim();
  const withScheme = /^https?:\/\//i.test(rawValue) ? rawValue : `http://${rawValue}`;
  return withScheme.replace(/\/+$/, "");
}

function setStatus(label, className) {
  els.connectionStatus.textContent = label;
  els.connectionStatus.closest(".status-pill").className = `status-pill ${className || ""}`.trim();
  els.activeBoardState.textContent = label;
  els.activeBoardState.className = className || "";
}

function selectedDeviceId() {
  return els.deviceId.value.trim();
}

async function requestJson(url, options = {}) {
  const requestOptions = {
    headers: {"Content-Type": "application/json"},
    ...options,
  };
  if (!isCloudMode() && els.shell.dataset.localProxy !== "true") {
    requestOptions.mode = "cors";
  }
  const response = await fetch(url, requestOptions);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || body.error || "Request failed");
  }
  return body;
}

function signalPercent(rssi) {
  const value = Number(rssi);
  if (!Number.isFinite(value)) {
    return null;
  }
  return Math.max(0, Math.min(100, Math.round((value + 100) * 2)));
}

function rawSignal(device) {
  const candidates = [device.rssi, device.wifi_rssi, device.signal];
  const value = candidates.find((candidate) => Number.isFinite(Number(candidate)));
  return value === undefined ? null : Number(value);
}

function deviceIp(device) {
  return device.ip || device.device_local_host || device.source_ip || device.local_ip || "";
}

function deviceLocalUrl(device) {
  return device.device_local_url || (deviceIp(device) ? `http://${deviceIp(device)}` : "");
}

function shortDateTime(value) {
  if (!value) {
    return {date: "--", time: ""};
  }
  if (Number.isFinite(Number(value))) {
    const numericValue = Number(value);
    const milliseconds = numericValue < 100000000000 ? numericValue * 1000 : numericValue;
    const date = new Date(milliseconds);
    return {
      date: date.toLocaleDateString(undefined, {year: "numeric", month: "2-digit", day: "2-digit"}),
      time: date.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", second: "2-digit"}),
    };
  }
  const normalized = String(value).includes("T") ? String(value) : String(value).replace(" ", "T");
  const date = new Date(normalized.endsWith("Z") ? normalized : `${normalized}Z`);
  if (Number.isNaN(date.getTime())) {
    const [rawDate, rawTime = ""] = String(value).split(/[ T]/);
    return {date: rawDate || "--", time: rawTime ? `${rawTime.replace(/\.\d+$/, "")}` : ""};
  }
  return {
    date: date.toLocaleDateString(undefined, {year: "numeric", month: "2-digit", day: "2-digit"}),
    time: date.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", second: "2-digit"}),
  };
}

function lastSyncValue(device) {
  return device.last_sync_at || device.last_seen_at || device.cloud_last_seen_at || device.cloud_last_seen || device.updated_at || "";
}

function telemetryStatus(device) {
  if (device.telemetry_status_label) {
    return device.telemetry_status_label;
  }
  return device.online ? "Live" : "Stale";
}

function statusTone(isReachable, isWarning = false) {
  if (isWarning) {
    return "warning";
  }
  return isReachable ? "online" : "offline";
}

function alertSummary(device, percent) {
  const count = Number(device.active_alert_count || 0);
  if (count > 0) {
    return {label: `${count} active`, tone: device.latest_alert_severity === "danger" ? "danger" : "warning"};
  }
  if (!device.online) {
    return {label: "1 active", tone: "danger"};
  }
  if (percent !== null && percent < 45) {
    return {label: "Signal", tone: "warning"};
  }
  return {label: "Clear", tone: "clear"};
}

function normalizeChannels(channels) {
  return Array.isArray(channels) ? channels : [];
}

function applianceLabel(channel) {
  return channel.name || channel.label || `Appliance ${channel.id ?? ""}`.trim();
}

function renderApplianceList(device) {
  const channels = normalizeChannels(device.channels);
  if (channels.length) {
    return `
      <div class="appliance-list">
        ${channels
          .map((channel) => {
            const isOn = Boolean(channel.state);
            return `
              <span class="appliance-chip ${isOn ? "on" : "off"}">
                <strong>${escapeHtml(applianceLabel(channel))}</strong>
                <em>${isOn ? "On" : "Off"}</em>
              </span>
            `;
          })
          .join("")}
      </div>
    `;
  }

  const channelCount = Number.isFinite(Number(device.channel_count)) ? Number(device.channel_count) : 0;
  if (channelCount > 0) {
    return `
      <div class="appliance-list">
        <span class="appliance-chip unknown">
          <strong>${channelCount} appliance${channelCount === 1 ? "" : "s"}</strong>
          <em>Waiting for names</em>
        </span>
      </div>
    `;
  }

  return `
    <div class="appliance-list">
      <span class="appliance-chip unknown">
        <strong>No appliances</strong>
        <em>No telemetry</em>
      </span>
    </div>
  `;
}

function updateDeviceFromStatus(device, data) {
  device.online = true;
  device.ip = data.ip || device.ip || "";
  device.rssi = data.rssi;
  device.fan_speed = data.fan_speed;
  device.channels = normalizeChannels(data.channels);
  device.cloud_last_seen = data.cloud_last_seen || Math.floor(Date.now() / 1000);
  device.channel_count = device.channels.length;
}

function dashboardStats() {
  const total = state.devices.length;
  const online = state.devices.filter((device) => device.online).length;
  const offline = Math.max(0, total - online);
  const activeCustomers = state.devices.filter((device) => device.has_credentials).length;
  const lowSignal = state.devices.filter((device) => {
    const percent = signalPercent(rawSignal(device));
    return device.online && percent !== null && percent < 45;
  }).length;
  const alerts = offline + lowSignal;
  return {total, online, offline, activeCustomers, lowSignal, alerts};
}

function renderDashboardStats() {
  const stats = dashboardStats();
  els.totalBoardsKpi.textContent = String(stats.total);
  els.onlineBoardsKpi.textContent = String(stats.online);
  els.offlineBoardsKpi.textContent = String(stats.offline);
  els.activeCustomersKpi.textContent = String(stats.activeCustomers);
  els.alertsKpi.textContent = String(stats.alerts);
  els.onlineStatusText.textContent = String(stats.online);
  els.offlineStatusText.textContent = String(stats.offline);
  els.lowSignalText.textContent = String(stats.lowSignal);

  const onlinePct = stats.total ? Math.round((stats.online / stats.total) * 100) : 0;
  els.onlineBar.style.width = `${onlinePct}%`;
  els.onlineCaption.textContent = stats.total ? `${onlinePct}% of registered boards are online` : "No boards registered yet";

  renderAlerts(stats);
}

function renderAlerts(stats) {
  els.alertList.innerHTML = "";
  const alerts = [];
  if (stats.offline) {
    alerts.push(["Board Offline", `${stats.offline} board${stats.offline === 1 ? "" : "s"} need attention`]);
  }
  if (stats.lowSignal) {
    alerts.push(["Low Signal", `${stats.lowSignal} online board${stats.lowSignal === 1 ? "" : "s"} below 45% signal`]);
  }
  if (!alerts.length) {
    alerts.push(["No Active Alerts", "All known boards are healthy"]);
  }
  alerts.forEach(([title, detail]) => {
    const item = document.createElement("div");
    item.className = "alert-item";
    item.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span>`;
    els.alertList.appendChild(item);
  });
}

function filteredDevices() {
  const query = (els.boardSearch?.value || "").trim().toLowerCase();
  const filter = els.boardFilter?.value || "all";
  return state.devices.filter((device) => {
    const haystack = [
      device.device_id,
      device.label,
      device.email,
      device.access_label,
      device.credentials_label,
    ].join(" ").toLowerCase();
    const matchesQuery = !query || haystack.includes(query);
    const percent = signalPercent(rawSignal(device));
    const isWarning = device.online && percent !== null && percent < 45;
    const matchesFilter =
      filter === "all" ||
      (filter === "online" && device.online) ||
      (filter === "offline" && !device.online) ||
      (filter === "warning" && isWarning);
    return matchesQuery && matchesFilter;
  });
}

function renderBoardTable() {
  if (!els.boardTableBody) {
    return;
  }
  els.boardTableBody.innerHTML = "";
  const devices = filteredDevices();
  if (els.boardTableMeta) {
    const total = state.devices.length;
    els.boardTableMeta.textContent = `${total} device${total === 1 ? "" : "s"}`;
  }
  if (els.boardVisiblePill) {
    els.boardVisiblePill.textContent = `${devices.length} visible`;
  }
  if (els.boardUpdatedPill) {
    els.boardUpdatedPill.textContent = "Updated now";
  }
  if (!devices.length) {
    const row = document.createElement("tr");
    row.innerHTML = `<td class="empty-row" colspan="12">No boards match the current view.</td>`;
    els.boardTableBody.appendChild(row);
    return;
  }

  const csrfToken = document.querySelector("input[name='csrf_token']")?.value || "";
  devices.forEach((device) => {
    const rssi = rawSignal(device);
    const percent = signalPercent(rssi);
    const statusClass = device.online ? (percent !== null && percent < 45 ? "warning" : "online") : "offline";
    const masterText = device.online ? "Reachable" : "Unreachable";
    const slaveText = device.slave_status_label || "Disabled";
    const slaveTone = device.slave_status_tone || "clear";
    const upperText = device.upper_sensor_status_label || masterText;
    const upperTone = device.upper_sensor_status_tone || statusTone(device.online, statusClass === "warning");
    const lowerText = device.lower_sensor_status_label || "Disabled";
    const lowerTone = device.lower_sensor_status_tone || "clear";
    const alert = alertSummary(device, percent);
    const lastSync = shortDateTime(lastSyncValue(device));
    const signalLabel = rssi === null ? "--" : String(rssi);
    const customerLabel = device.label || device.email || "Unassigned";
    const customerDetail = device.email && device.label ? device.email : device.access_label || device.credentials_label || "";
    const localIp = deviceIp(device);
    const localUrl = deviceLocalUrl(device);
    const row = document.createElement("tr");
    row.dataset.deviceId = device.device_id;
    if (alert.tone === "danger" || alert.tone === "warning") {
      row.classList.add("has-alerts");
    }
    if (device.device_id === selectedDeviceId()) {
      row.classList.add("selected-row");
    }
    row.innerHTML = `
      <td class="device-id-cell">
        <span class="board-id">${escapeHtml(device.device_id)}</span>
        <span class="cell-sub">Local IP: ${
          localUrl
            ? `<a class="device-link" href="${escapeHtml(localUrl)}" target="_blank" rel="noopener">${escapeHtml(localIp || localUrl)}</a>`
            : "--"
        }</span>
      </td>
      <td>
        <span class="cell-main">${escapeHtml(customerLabel)}</span>
        <span class="cell-sub">${escapeHtml(customerDetail || "Email not added")}</span>
      </td>
      <td><span class="status-badge ${statusClass}">${masterText}</span></td>
      <td><span class="status-badge ${slaveTone}">${escapeHtml(slaveText)}</span></td>
      <td>${escapeHtml(telemetryStatus(device))}</td>
      <td>${escapeHtml(signalLabel)}</td>
      <td><span class="status-badge ${upperTone}">${escapeHtml(upperText)}</span></td>
      <td><span class="status-badge ${lowerTone}">${escapeHtml(lowerText)}</span></td>
      <td>${renderApplianceList(device)}</td>
      <td><span class="status-badge ${alert.tone}">${escapeHtml(alert.label)}</span></td>
      <td class="last-sync-cell ${device.online ? "is-live" : "is-stale"}">
        <span class="sync-date">${escapeHtml(lastSync.date)}</span>
        ${lastSync.time ? `<span class="sync-time">${escapeHtml(lastSync.time)}</span>` : ""}
      </td>
      <td><div class="row-actions"></div></td>
    `;
    const actions = row.querySelector(".row-actions");
    const view = document.createElement("button");
    view.className = "link-button";
    view.type = "button";
    view.textContent = "View";
    view.addEventListener("click", () => selectRegisteredDevice(device.device_id));
    actions.appendChild(view);

    if (device.can_delete) {
      const form = document.createElement("form");
      form.method = "post";
      form.action = `/admin/home-automation/${encodeURIComponent(device.device_id)}/delete`;
      form.addEventListener("submit", (event) => {
        if (!confirm(`Delete SHA device ${device.device_id} and its stored admin records?`)) {
          event.preventDefault();
        }
      });
      form.innerHTML = `
        <input type="hidden" name="csrf_token" value="${escapeHtml(csrfToken)}">
        <button class="secondary danger-action" type="submit">Delete</button>
      `;
      actions.appendChild(form);
    }

    els.boardTableBody.appendChild(row);
  });
}

function renderDeviceOptions() {
  if (!els.registeredDeviceSelect) {
    return;
  }
  els.registeredDeviceSelect.innerHTML = "";
  if (!state.devices.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No boards";
    els.registeredDeviceSelect.appendChild(option);
    return;
  }
  state.devices.forEach((device) => {
    const option = document.createElement("option");
    option.value = device.device_id;
    option.textContent = device.label || device.device_id;
    els.registeredDeviceSelect.appendChild(option);
  });
  if (!selectedDeviceId()) {
    els.deviceId.value = state.devices[0].device_id;
  }
  els.registeredDeviceSelect.value = selectedDeviceId();
}

function renderRegisteredDevices() {
  renderDeviceOptions();
  renderDashboardStats();
  renderBoardTable();
  renderSelectedBoardHeader();
}

async function refreshFleetStatuses() {
  if (!isCloudMode() || !state.devices.length) {
    return;
  }
  const selectedId = selectedDeviceId();
  const results = await Promise.allSettled(
    state.devices.map(async (device) => {
      const data = await requestJson(cloudApiPath(device.device_id, "status"));
      return {device, data};
    })
  );

  results.forEach((result) => {
    if (result.status === "fulfilled") {
      updateDeviceFromStatus(result.value.device, result.value.data);
      if (result.value.device.device_id === selectedId) {
        state.channels = normalizeChannels(result.value.data.channels);
        state.fanSpeed = Number.isFinite(result.value.data.fan_speed) ? result.value.data.fan_speed : 0;
      }
      return;
    }
  });

  renderRegisteredDevices();
}

function selectRegisteredDevice(deviceId) {
  if (!deviceId) {
    return;
  }
  els.deviceId.value = deviceId;
  if (els.registeredDeviceSelect) {
    els.registeredDeviceSelect.value = deviceId;
  }
  renderRegisteredDevices();
  document.getElementById("selectedBoard")?.scrollIntoView({behavior: "smooth", block: "start"});
  refreshStatus();
}

function renderSelectedBoardHeader() {
  const device = state.devices.find((item) => item.device_id === selectedDeviceId());
  const label = device?.label || selectedDeviceId() || "No board selected";
  els.activeBoardName.textContent = label;
  els.selectedBoardTitle.textContent = label;
}

function markSelectedDeviceOnline(data) {
  const deviceId = selectedDeviceId();
  const device = state.devices.find((item) => item.device_id === deviceId);
  if (!device) {
    return;
  }
  updateDeviceFromStatus(device, data);
  renderRegisteredDevices();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderStatus(data) {
  if (!Array.isArray(data.channels)) {
    throw new Error(data.error || "home_status_unavailable");
  }

  state.channels = Array.isArray(data.channels) ? data.channels : [];
  state.fanSpeed = Number.isFinite(data.fan_speed) ? data.fan_speed : 0;

  const percent = signalPercent(data.rssi);
  els.modeLabel.textContent = isCloudMode() ? "Cloud" : "Local Wi-Fi";
  els.ipLabel.textContent = data.ip || "-";
  els.rssiLabel.textContent = data.rssi === undefined ? "-" : `${data.rssi} dBm`;
  els.fanLabel.textContent = String(state.fanSpeed);
  els.fanSpeed.value = String(state.fanSpeed);
  els.fanSpeedText.textContent = `Speed ${state.fanSpeed}`;
  els.signalBar.style.width = `${percent || 0}%`;
  els.signalCaption.textContent = percent === null ? "Waiting for telemetry" : `${percent}% estimated signal strength`;
  markSelectedDeviceOnline(data);
  renderActivity(`Status refreshed for ${selectedDeviceId()}`, data.ip || "cloud");
  renderChannels();
}

function renderChannels() {
  els.channels.innerHTML = "";
  if (!state.channels.length) {
    const detail = document.createElement("div");
    detail.className = "notice neutral";
    detail.textContent = "No appliance telemetry has been received for this board yet.";
    els.channels.appendChild(detail);
    return;
  }

  state.channels.forEach((channel) => {
    const applianceName = channel.name || `Appliance ${channel.id}`;
    const isOn = Boolean(channel.state);
    const card = document.createElement("article");
    card.className = `channel-card ${isOn ? "is-on" : "is-off"}`;
    card.innerHTML = `
      <div>
        <div class="channel-name">${escapeHtml(applianceName)}</div>
        <div class="channel-state ${isOn ? "on" : "off"}">${isOn ? "Running" : "Off"}</div>
      </div>
      <div class="toggle">
        <span class="toggle-switch ${isOn ? "on" : ""}" aria-hidden="true"></span>
        <button data-id="${channel.id}" data-state="${isOn ? "off" : "on"}" type="button">${isOn ? "Turn Off" : "Turn On"}</button>
      </div>
    `;
    els.channels.appendChild(card);
  });
}

function renderActivity(message, detail) {
  const item = document.createElement("div");
  item.className = "timeline-item";
  item.innerHTML = `<strong>${escapeHtml(message)}</strong><span>${escapeHtml(detail || new Date().toLocaleTimeString())}</span>`;
  els.activityTimeline.prepend(item);
  while (els.activityTimeline.children.length > 5) {
    els.activityTimeline.lastElementChild.remove();
  }
}

async function refreshStatus() {
  if (isCloudMode() && !selectedDeviceId()) {
    setStatus("No device", "offline");
    return;
  }

  setStatus("Checking", "");
  try {
    const data = await requestJson(apiPath("status"));
    renderStatus(data);
    setStatus("Online", "online");
  } catch (error) {
    setStatus("Offline", "offline");
    renderError(error);
  }
}

function localCommandOptions(payload) {
  if (isCloudMode() || els.shell.dataset.localProxy === "true") {
    return {
      method: "POST",
      body: JSON.stringify(payload),
    };
  }
  const query = new URLSearchParams(payload).toString();
  return {
    method: "GET",
    urlSuffix: query ? `?${query}` : "",
  };
}

async function requestCommand(path, payload) {
  const options = localCommandOptions(payload);
  const url = `${apiPath(path)}${options.urlSuffix || ""}`;
  delete options.urlSuffix;
  await requestJson(url, options);
  renderActivity(`Command sent: ${path}`, selectedDeviceId());
  await refreshStatus();
}

async function sendAll(nextState) {
  await requestCommand("all", {state: nextState});
}

async function sendFan(speed) {
  await requestCommand("fan", {speed});
}

async function sendSwitch(id, nextState) {
  await requestCommand("switch", {id, state: nextState});
}

function renderError(error) {
  els.signalBar.style.width = "0";
  els.signalCaption.textContent = "Board is offline or unreachable";
  els.channels.innerHTML = "";
  const detail = document.createElement("div");
  detail.className = "notice";
  detail.textContent = isCloudMode()
    ? `Cloud status is unavailable for ${selectedDeviceId() || "this device"}. ${error.message || ""}`
    : `Local device is unreachable from this browser. Use the same Wi-Fi as the board and confirm the URL ${normalizedLocalUrl()}. ${error.message || ""}`;
  els.channels.appendChild(detail);
  renderActivity("Status check failed", selectedDeviceId() || "No board selected");
}

els.channels.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-id]");
  if (!button) {
    return;
  }
  await sendSwitch(button.dataset.id, button.dataset.state);
});

els.mode.addEventListener("change", refreshStatus);
els.deviceId.addEventListener("change", () => {
  renderSelectedBoardHeader();
  refreshStatus();
});
els.registeredDeviceSelect.addEventListener("change", () => selectRegisteredDevice(els.registeredDeviceSelect.value));
els.boardSearch?.addEventListener("input", renderBoardTable);
els.boardFilter?.addEventListener("change", renderBoardTable);
els.boardSearch?.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && els.boardSearch.value) {
    els.boardSearch.value = "";
    renderBoardTable();
  }
});
els.refreshButton.addEventListener("click", refreshStatus);
els.allOnButton.addEventListener("click", () => sendAll("on"));
els.allOffButton.addEventListener("click", () => sendAll("off"));
els.fanSpeed.addEventListener("input", () => {
  els.fanSpeedText.textContent = `Speed ${els.fanSpeed.value}`;
});
els.fanSpeed.addEventListener("change", () => sendFan(els.fanSpeed.value));

renderRegisteredDevices();
renderActivity("Dashboard opened", selectedDeviceId() || "No board selected");
refreshStatus();
refreshFleetStatuses();
setInterval(refreshStatus, 10000);
setInterval(refreshFleetStatuses, 30000);
