from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")


def function_source(name, next_name):
    start = SERVER_SOURCE.index(f"def {name}(")
    end = SERVER_SOURCE.index(f"def {next_name}(", start)
    return SERVER_SOURCE[start:end]


def test_successful_firmware_upload_replaces_same_device_role_history():
    source = function_source("create_firmware_artifact", "ensure_android_release_dir")

    assert "SELECT id, stored_filename" in source
    assert "WHERE target_device = ? AND target_role = ?" in source
    assert "DELETE FROM firmware_artifacts" in source
    assert "AND id <> ?" in source
    assert "if old_path == active_path" in source
    assert source.index("INSERT INTO firmware_artifacts") < source.index("DELETE FROM firmware_artifacts")


def test_successful_android_upload_replaces_all_release_history():
    source = function_source("create_android_app_release", "remove_all_android_app_releases")

    assert "SELECT id, stored_filename" in source
    assert "DELETE FROM android_app_releases" in source
    assert "WHERE id <> ?" in source
    assert "if old_path == active_path" in source
    assert source.index("INSERT INTO android_app_releases") < source.index("DELETE FROM android_app_releases")

