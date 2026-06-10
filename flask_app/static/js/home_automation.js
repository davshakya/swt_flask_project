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
  registeredDevices: document.getElementById("registeredDevices"),
  connectionStatus: document.getElementById("connectionStatus"),
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
    const deviceId = encodeURIComponent(els.deviceId.value.trim());
    return `/api/home-automation/cloud/${deviceId}/${path}`;
  }
  if (els.shell.dataset.localProxy === "true") {
    return `/api/home-automation/local/${path}`;
  }
  return `${normalizedLocalUrl()}/${path}`;
}

function normalizedLocalUrl() {
  const rawValue = document.getElementById("localUrl").value.trim();
  const withScheme = /^https?:\/\//i.test(rawValue) ? rawValue : `http://${rawValue}`;
  return withScheme.replace(/\/+$/, "");
}

function setStatus(label, className) {
  els.connectionStatus.textContent = label;
  els.connectionStatus.className = `status-pill ${className || ""}`.trim();
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

function renderStatus(data) {
  if (!Array.isArray(data.channels)) {
    throw new Error(data.error || "home_status_unavailable");
  }

  state.channels = Array.isArray(data.channels) ? data.channels : [];
  state.fanSpeed = Number.isFinite(data.fan_speed) ? data.fan_speed : 0;

  els.modeLabel.textContent = isCloudMode() ? "Cloud" : "Local Wi-Fi";
  els.ipLabel.textContent = data.ip || "-";
  els.rssiLabel.textContent = data.rssi === undefined ? "-" : `${data.rssi} dBm`;
  els.fanLabel.textContent = String(state.fanSpeed);
  els.fanSpeed.value = String(state.fanSpeed);
  els.fanSpeedText.textContent = `Speed ${state.fanSpeed}`;
  markSelectedDeviceOnline(data);
  els.channels.innerHTML = "";

  state.channels.forEach((channel) => {
    const card = document.createElement("article");
    card.className = "channel-card";
    card.innerHTML = `
      <div>
        <div class="channel-name">${escapeHtml(channel.name || `Channel ${channel.id}`)}</div>
        <div class="channel-state">${channel.state ? "On" : "Off"}</div>
      </div>
      <div class="toggle">
        <button class="off ${channel.state ? "" : "active"}" data-id="${channel.id}" data-state="off" type="button">Off</button>
        <button class="on ${channel.state ? "active" : ""}" data-id="${channel.id}" data-state="on" type="button">On</button>
      </div>
    `;
    els.channels.appendChild(card);
  });
}

function renderRegisteredDevices() {
  if (!els.registeredDevices || !els.registeredDeviceSelect) {
    return;
  }

  els.registeredDeviceSelect.innerHTML = "";
  els.registeredDevices.innerHTML = "";

  if (!state.devices.length) {
    els.registeredDeviceSelect.hidden = true;
    const empty = document.createElement("div");
    empty.className = "notice neutral";
    empty.textContent = "No registered Home Automation boards are assigned to this login yet.";
    els.registeredDevices.appendChild(empty);
    return;
  }

  els.registeredDeviceSelect.hidden = state.devices.length < 2 && els.shell.dataset.canSelectDevices !== "true";
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

  state.devices.forEach((device) => {
    const card = document.createElement("button");
    card.className = `device-card ${device.online ? "online" : "offline"} ${device.device_id === selectedDeviceId() ? "selected" : ""}`;
    card.type = "button";
    card.dataset.deviceId = device.device_id;
    card.innerHTML = `
      <span class="device-card-top">
        <strong>${escapeHtml(device.label || device.device_id)}</strong>
        <span>${device.online ? "Online" : "Offline"}</span>
      </span>
      <span class="device-id">${escapeHtml(device.device_id)}</span>
      <span class="device-meta">${escapeHtml(device.access_label || "Registered device")}</span>
      <span class="device-meta">${escapeHtml(device.credentials_label || "No customer credentials")}${device.email ? ` · ${escapeHtml(device.email)}` : ""}</span>
      <span class="device-meta">${device.ip ? `IP ${escapeHtml(device.ip)}` : "Waiting for telemetry"}${device.rssi === null || device.rssi === undefined ? "" : ` · ${escapeHtml(device.rssi)} dBm`}</span>
    `;
    els.registeredDevices.appendChild(card);
  });
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
  refreshStatus();
}

function markSelectedDeviceOnline(data) {
  const deviceId = selectedDeviceId();
  const device = state.devices.find((item) => item.device_id === deviceId);
  if (!device) {
    return;
  }
  device.online = true;
  device.ip = data.ip || device.ip || "";
  device.rssi = data.rssi;
  device.fan_speed = data.fan_speed;
  device.channel_count = Array.isArray(data.channels) ? data.channels.length : device.channel_count;
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

async function refreshStatus() {
  if (isCloudMode() && !els.deviceId.value.trim()) {
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
  els.channels.innerHTML = "";
  const detail = document.createElement("div");
  detail.className = "notice";
  detail.textContent = isCloudMode()
    ? `Cloud status is unavailable for ${els.deviceId.value.trim() || "this device"}. ${error.message || ""}`
    : `Local device is unreachable from this browser. Use the same Wi-Fi as the board and confirm the URL ${normalizedLocalUrl()}. ${error.message || ""}`;
  els.channels.appendChild(detail);
}

els.channels.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-id]");
  if (!button) {
    return;
  }
  await sendSwitch(button.dataset.id, button.dataset.state);
});

els.mode.addEventListener("change", refreshStatus);
els.deviceId.addEventListener("change", refreshStatus);
els.registeredDeviceSelect.addEventListener("change", () => selectRegisteredDevice(els.registeredDeviceSelect.value));
els.registeredDevices.addEventListener("click", (event) => {
  const card = event.target.closest(".device-card");
  if (card) {
    selectRegisteredDevice(card.dataset.deviceId);
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
refreshStatus();
setInterval(refreshStatus, 10000);
