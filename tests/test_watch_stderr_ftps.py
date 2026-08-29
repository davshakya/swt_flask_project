from argparse import Namespace
import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "watch_stderr_ftps.py"
SPEC = importlib.util.spec_from_file_location("watch_stderr_ftps", SCRIPT_PATH)
WATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCHER)


def watcher_args():
    return Namespace(
        host="ftp.example.test",
        port=21,
        user="operator",
        remote_file="stderr.log",
        allow_multiple=False,
    )


def test_watcher_prevents_duplicate_local_instance():
    first_lock = WATCHER.acquire_single_instance(watcher_args())
    try:
        with pytest.raises(SystemExit, match="already running"):
            WATCHER.acquire_single_instance(watcher_args())
    finally:
        first_lock.close()


def test_watcher_can_explicitly_allow_multiple_instances():
    args = watcher_args()
    args.allow_multiple = True
    assert WATCHER.acquire_single_instance(args) is None
