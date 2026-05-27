from __future__ import annotations

import atexit
import os
import shutil
import stat
import subprocess
import time
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
MYSQL_ROOT = Path(r"C:\Program Files\MySQL\MySQL Server 8.0")
MYSQLD = MYSQL_ROOT / "bin" / "mysqld.exe"
MYSQL_DATA_DIR = PROJECT_ROOT.parent / "mysql-data"
MYSQL_STDOUT_LOG = PROJECT_ROOT.parent / "mysql-local.out.log"
MYSQL_STDERR_LOG = PROJECT_ROOT.parent / "mysql-local.err.log"


def _mysql_is_listening() -> bool:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "if (Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _start_local_mysql_if_available() -> None:
    if _mysql_is_listening() or not MYSQLD.exists():
        return

    MYSQL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not (MYSQL_DATA_DIR / "mysql").exists():
        subprocess.run(
            [
                str(MYSQLD),
                "--no-defaults",
                "--initialize-insecure",
                f"--basedir={MYSQL_ROOT}",
                f"--datadir={MYSQL_DATA_DIR}",
            ],
            timeout=120,
            check=True,
        )

    args = [
        str(MYSQLD),
        "--no-defaults",
        f"--basedir={MYSQL_ROOT}",
        f"--datadir={MYSQL_DATA_DIR}",
        "--port=3306",
        "--bind-address=127.0.0.1",
        "--console",
    ]
    with MYSQL_STDOUT_LOG.open("ab") as stdout, MYSQL_STDERR_LOG.open("ab") as stderr:
        subprocess.Popen(
            args,
            stdout=stdout,
            stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    deadline = time.time() + 30
    while time.time() < deadline:
        if _mysql_is_listening():
            return
        time.sleep(1)

    raise RuntimeError("Local MySQL did not start on 127.0.0.1:3306.")


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
    os.environ["DB_BACKEND"] = "mysql"
    os.environ["DATABASE_URL"] = ""
    os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
    os.environ.setdefault("MYSQL_PORT", "3306")
    os.environ.setdefault("MYSQL_USER", "root")
    os.environ.setdefault("MYSQL_PASSWORD", "")
    os.environ.setdefault("MYSQL_DATABASE", "swt_flask_test")
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-mysql-only-backend-2026")
    cleanup_test_artifacts()


def pytest_sessionfinish(session, exitstatus):
    cleanup_test_artifacts()


atexit.register(cleanup_test_artifacts)
