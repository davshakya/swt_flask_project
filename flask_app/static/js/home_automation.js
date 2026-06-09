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
  if (!isCloudMode()) {
    return `/api/home-automation/local/${path}`;
  }
  const deviceId = encodeURIComponent(els.deviceId.value.trim());
  return `/api/home-automation/cloud/${deviceId}/${path}`;
}

function setStatus(label, className) {
  els.connectionStatus.textContent = label;
  els.connectionStatus.className = `status-pill ${className || ""}`.trim();
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {"Content-Type": "application/json"},
    ...options,
  });
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
  }
}

async function sendSwitch(id, nextState) {
  await requestJson(apiPath("switch"), {
    method: "POST",
    body: JSON.stringify({id, state: nextState}),
  });
  await refreshStatus();
}

async function sendAll(nextState) {
  await requestJson(apiPath("all"), {
    method: "POST",
    body: JSON.stringify({state: nextState}),
  });
  await refreshStatus();
}

async function sendFan(speed) {
  await requestJson(apiPath("fan"), {
    method: "POST",
    body: JSON.stringify({speed}),
  });
  await refreshStatus();
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

if (els.shell.dataset.cloudEnabled !== "true") {
  const option = els.mode.querySelector('option[value="cloud"]');
  option.textContent = "Cloud (not configured)";
}

refreshStatus();
setInterval(refreshStatus, 10000);
