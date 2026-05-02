from __future__ import annotations

import atexit
import os
import shutil
import stat
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
PYTEST_CACHE_FILE_DIR_GLOB = "pytest-cache-files-*"
TEST_ARTIFACT_GLOBS = (
    PYTEST_CACHE_FILE_DIR_GLOB,
    ".pytest_cache",
    ".pytest-tmp",
    ".tmp",
)
TEST_ARTIFACT_FILES = (
    PROJECT_ROOT / "data" / "startup-invalid-env.db",
    PROJECT_ROOT / "data" / "startup-invalid-env.db-shm",
    PROJECT_ROOT / "data" / "startup-invalid-env.db-wal",
    PROJECT_ROOT / "data" / "test-startup-env.db",
    PROJECT_ROOT / "data" / "test-startup-env.db-shm",
    PROJECT_ROOT / "data" / "test-startup-env.db-wal",
)


def _remove_readonly(func, path, _exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, onerror=_remove_readonly)
        elif path.exists():
            path.unlink()
    except OSError:
        # Best effort cleanup for pytest temp/cache spillover.
        pass


def cleanup_test_artifacts() -> None:
    for pattern in TEST_ARTIFACT_GLOBS:
        for path in PROJECT_ROOT.glob(pattern):
            _remove_path(path)
    for path in TEST_ARTIFACT_FILES:
        try:
            _remove_path(path)
        except OSError:
            pass


def pytest_sessionstart(session):
    os.environ["SWT_ALLOW_SQLITE_FOR_TESTS"] = "1"
    os.environ["DB_BACKEND"] = "sqlite"
    os.environ["DATABASE_URL"] = ""
    cleanup_test_artifacts()


def pytest_sessionfinish(session, exitstatus):
    cleanup_test_artifacts()


atexit.register(cleanup_test_artifacts)
