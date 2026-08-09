from __future__ import annotations

from collections import OrderedDict
import threading
import time

from flask_app.capacity_state import normalize_sequence_number


class DeviceTokenBucketLimiter:
    def __init__(self, rate_per_second=2.0, burst=5, max_devices=5000):
        self.rate = max(0.1, float(rate_per_second))
        self.burst = max(1, int(burst))
        self.max_devices = max(100, int(max_devices))
        self._buckets = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, device_id, now=None):
        current = time.monotonic() if now is None else float(now)
        key = str(device_id or "").strip().lower()
        with self._lock:
            tokens, updated_at = self._buckets.pop(key, (float(self.burst), current))
            tokens = min(float(self.burst), tokens + max(0.0, current - updated_at) * self.rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, current)
            while len(self._buckets) > self.max_devices:
                self._buckets.popitem(last=False)
            retry_after = 0.0 if allowed else max(0.05, (1.0 - tokens) / self.rate)
            return allowed, retry_after


def sequence_status(cursor, device_id, device_source, payload):
    boot_id = str(payload.get("boot_id") or payload.get("pump_runtime_boot_id") or "").strip()
    sequence_number = normalize_sequence_number(payload)
    if not boot_id or sequence_number is None:
        return "missing"
    existing = cursor.execute(
        """
        SELECT boot_id, sequence_number
        FROM device_latest_state
        WHERE device_id = ? AND device_source = ?
        """,
        (device_id, device_source),
    ).fetchone()
    if not existing or str(existing.get("boot_id") or "") != boot_id:
        return "new"
    previous = existing.get("sequence_number")
    if previous is None or sequence_number > int(previous):
        return "new"
    return "duplicate" if sequence_number == int(previous) else "replay"
