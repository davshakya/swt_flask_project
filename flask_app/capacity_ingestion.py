from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class DeviceIngestionResult:
    telemetry: dict[str, Any]
    outcome: str

    @property
    def saved(self):
        return self.outcome == "saved"

    @property
    def duplicate(self):
        return self.outcome == "duplicate"

    @property
    def ignored(self):
        return self.outcome == "ignored"


def ingest_device_payload(
    payload: Mapping[str, Any],
    *,
    processor: Callable[..., dict[str, Any]],
    authenticated_device_id: str | None = None,
    source_ip: str | None = None,
    transport: str = "http",
    defer_postprocess: bool = False,
):
    """Run one payload through the existing idempotent telemetry processor.

    Authentication remains at the transport boundary. Supplying the authenticated
    identity here prevents a caller from accidentally persisting a body-provided
    device ID after authentication has succeeded.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("device telemetry payload must be a mapping")
    normalized_payload = dict(payload)
    if authenticated_device_id:
        normalized_payload["device_id"] = authenticated_device_id
    cleaned = processor(
        normalized_payload,
        source_ip=source_ip,
        transport=transport,
        defer_postprocess=defer_postprocess,
    )
    if not isinstance(cleaned, dict):
        raise TypeError("device telemetry processor must return a dictionary")
    outcome = str(cleaned.get("_telemetry_sync_result") or "saved").strip().lower()
    if outcome not in {"saved", "duplicate", "ignored"}:
        outcome = "saved"
    return DeviceIngestionResult(telemetry=cleaned, outcome=outcome)
