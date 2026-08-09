from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_capacity_maintenance.py"


def load_script():
    spec = importlib.util.spec_from_file_location("capacity_maintenance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cron_retention_script_is_bounded_and_non_overlapping():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "default=240" in source
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in source
    assert '"retention": ("cron_retention",)' in source
    assert "maybe_prune_retained_rows(force=True)" in source


def test_cron_runner_exposes_separate_feature_gated_aggregation_tasks():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"legacy-archive"' in source
    assert '"hourly": ("hourly_aggregation",)' in source
    assert '"daily": ("daily_aggregation",)' in source
    assert '"archive": ("archival",)' in source
    assert '"legacy-archive": ("legacy_telemetry_archive",)' in source
    assert '"skipped": "legacy_writes_enabled"' in source
    assert '"jobs": ("cron_job_processor",)' in source


def test_cron_retention_disabled_is_a_successful_noop(tmp_path, monkeypatch, capsys):
    module = load_script()
    fake_server = SimpleNamespace(CAPACITY_FEATURES=SimpleNamespace(enabled=lambda _name: False))
    monkeypatch.setitem(__import__("sys").modules, "flask_app", SimpleNamespace(server=fake_server))

    assert module.run(["--lock-file", str(tmp_path / "retention.lock")]) == 0
    assert '"feature_disabled"' in capsys.readouterr().out


def test_legacy_archive_refuses_to_run_while_legacy_writes_are_enabled(
    tmp_path, monkeypatch, capsys
):
    module = load_script()
    fake_server = SimpleNamespace(CAPACITY_FEATURES=SimpleNamespace(enabled=lambda _name: True))
    monkeypatch.setitem(__import__("sys").modules, "flask_app", SimpleNamespace(server=fake_server))

    result = module.run(
        [
            "--task",
            "legacy-archive",
            "--lock-file",
            str(tmp_path / "legacy-archive.lock"),
        ]
    )

    assert result == 0
    assert '"legacy_writes_enabled"' in capsys.readouterr().out
