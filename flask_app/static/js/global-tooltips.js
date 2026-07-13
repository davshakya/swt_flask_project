(() => {
  "use strict";

  const explanations = {
    telemetry: "Telemetry is the latest operating data received from the device, including sensor, tank and pump readings.",
    rssi: "RSSI measures Wi-Fi signal strength in dBm. Values closer to 0 are stronger; below -70 dBm may be unreliable.",
    signal: "Signal shows Wi-Fi strength. A stronger connection improves telemetry and command reliability.",
    "municipal sensor": "Municipal Sensor detects availability from the incoming municipal or source-water supply when configured.",
    health: "Health summarizes device connectivity, telemetry freshness, sensors and active alerts. It is not a physical water-quality score.",
  };

  function applyTooltips(root = document) {
    root.querySelectorAll("[data-tooltip-key], th, label, dt, .metric-label, .card-label").forEach((node) => {
      if (node.title) return;
      const key = String(node.dataset.tooltipKey || node.textContent || "").trim().toLowerCase();
      const explanation = explanations[key];
      if (!explanation) return;
      node.title = explanation;
      node.setAttribute("aria-label", `${String(node.textContent || key).trim()}: ${explanation}`);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => applyTooltips());
  else applyTooltips();
  new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach((node) => {
    if (node.nodeType === 1) applyTooltips(node);
  }))).observe(document.documentElement, { childList: true, subtree: true });
})();
