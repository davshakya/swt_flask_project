from __future__ import annotations

import json


def fetch_latest_state_payload(cursor, device_id, device_source):
    row = cursor.execute(
        """
        SELECT state_json, received_at
        FROM device_latest_state
        WHERE device_id = ? AND device_source = ?
        LIMIT 1
        """,
        (device_id, device_source),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row.get("state_json") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    payload["device_id"] = device_id
    payload["device_source"] = device_source
    payload["created_at"] = row.get("received_at")
    payload["received_at"] = row.get("received_at")
    return payload


def fetch_narrow_history_rows(cursor, device_id, device_source, limit, start_at=None, end_at=None):
    clauses = ["device_id = ?", "device_source = ?"]
    params = [device_id, device_source]
    if start_at is not None:
        clauses.append("recorded_at >= ?")
        params.append(start_at)
    if end_at is not None:
        clauses.append("recorded_at < ?")
        params.append(end_at)
    params.append(max(1, int(limit)))
    return cursor.execute(
        f"""
        SELECT level, lower_tank_level, motor, sensor, recorded_at
        FROM tank_telemetry_history
        WHERE {' AND '.join(clauses)}
        ORDER BY recorded_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
