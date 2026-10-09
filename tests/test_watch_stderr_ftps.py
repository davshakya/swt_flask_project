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


def test_failed_snapshot_preserves_previous_log(tmp_path, monkeypatch):
    target = tmp_path / 'stderr.log'
    target.write_text('previous complete snapshot', encoding='utf-8')
    args = Namespace(interval=2, tail_bytes=100, plain_ftp=False, insecure_ftps=False,
                     once=True, output=target, allow_multiple=True, password_env='TEST_FTP_PASSWORD',
                     host='example.test', port=21, user='test', remote_file='stderr.log', from_start=False)
    class FTP:
        def retrbinary(self, command, callback, **kwargs):
            callback(b'incomplete new snapshot')
            raise ConnectionResetError('connection lost')
        def quit(self): pass
    monkeypatch.setattr(WATCHER, 'parse_args', lambda: args)
    monkeypatch.setattr(WATCHER, 'connect', lambda *a: FTP())
    monkeypatch.setattr(WATCHER, 'remote_size', lambda *a: 100)
    monkeypatch.setenv('TEST_FTP_PASSWORD', 'test-only')
    assert WATCHER.main() == 1
    assert target.read_text(encoding='utf-8') == 'previous complete snapshot'
    assert list(tmp_path.glob('*.tmp')) == []
