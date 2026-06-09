const state = {
  channels: [],
  fanSpeed: 0,
};

const els = {
  shell: document.querySelector(".shell"),
  mode: document.getElementById("mode"),
  deviceId: document.getElementById("deviceId"),
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
  state.channels = Array.isArray(data.channels) ? data.channels : [];
  state.fanSpeed = Number.isFinite(data.fan_speed) ? data.fan_speed : 0;

  els.modeLabel.textContent = isCloudMode() ? "Cloud" : "Local Wi-Fi";
  els.ipLabel.textContent = data.ip || "-";
  els.rssiLabel.textContent = data.rssi === undefined ? "-" : `${data.rssi} dBm`;
  els.fanLabel.textContent = String(state.fanSpeed);
  els.fanSpeed.value = String(state.fanSpeed);
  els.fanSpeedText.textContent = `Speed ${state.fanSpeed}`;
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
els.refreshButton.addEventListener("click", refreshStatus);
els.allOnButton.addEventListener("click", () => sendAll("on"));
els.allOffButton.addEventListener("click", () => sendAll("off"));
els.fanSpeed.addEventListener("input", () => {
  els.fanSpeedText.textContent = `Speed ${els.fanSpeed.value}`;
});
els.fanSpeed.addEventListener("change", () => sendFan(els.fanSpeed.value));

refreshStatus();
setInterval(refreshStatus, 10000);
