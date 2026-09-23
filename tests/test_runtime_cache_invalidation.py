"""Exercise cache invalidation without importing the database-starting server."""
import ast
from pathlib import Path


def load_invalidator():
    source = Path(__file__).resolve().parents[1] / "flask_app" / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "clear_runtime_caches")
    forgotten = []
    namespace = {
        "normalize_device_id": lambda value: str(value or "").strip(),
        "analytics_cache": {},
        "dashboard_snapshot_cache": {},
        "forget_alert_touches_for_device": lambda device=None: forgotten.append(device),
        "DEVICE_SOURCE_REAL": "real",
        "DEVICE_SOURCE_VIRTUAL": "virtual",
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace, forgotten


def test_device_update_preserves_other_customers_cached_analytics():
    state, forgotten = load_invalidator()
    cache = state["analytics_cache"]
    for device in ("tank-a", "tank-b", "*"):
        for mode in ("real", "virtual"):
            for end in ("2026-09-23", "2026-09-24"):
                cache[("2026-09-22", end, device, mode, "v1")] = object()
    retained = {key: value for key, value in cache.items() if key[2] == "tank-b"}
    snapshots = state["dashboard_snapshot_cache"]
    snapshots.update({f"{mode}:{device}": object()
                      for mode in ("real", "virtual")
                      for device in ("tank-a", "tank-b", "__latest__")})
    state["clear_runtime_caches"]("tank-a")
    assert cache == retained
    assert set(snapshots) == {"real:tank-b", "virtual:tank-b"}
    assert forgotten == ["tank-a"]


def test_global_invalidation_still_clears_every_device():
    state, forgotten = load_invalidator()
    state["analytics_cache"][("start", "end", "tank-a", "real", "v1")] = object()
    state["dashboard_snapshot_cache"]["real:tank-a"] = object()
    state["clear_runtime_caches"]()
    assert state["analytics_cache"] == {}
    assert state["dashboard_snapshot_cache"] == {}
    assert forgotten == [None]
