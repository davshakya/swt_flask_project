"""Coordinate optional background work across workers without holding MySQL."""
from contextlib import contextmanager
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import time
import threading

_local_guard = threading.Lock()
_local_work = {}
_warned_paths = set()


@contextmanager
def _local_lease(key, signature, interval, clock):
    with _local_guard:
        state = _local_work.setdefault(key, {"lock": threading.Lock()})
    if not state["lock"].acquire(blocking=False):
        yield False, 0.5
        return
    try:
        elapsed = clock() - state.get("completed_at", 0)
        if state.get("signature") == signature and 0 <= elapsed < interval:
            yield False, interval - elapsed
            return
        yield True, 0
        state.update(completed_at=clock(), signature=signature)
    finally:
        state["lock"].release()

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt


@contextmanager
def background_lease(key, signature, interval=30, directory=None, clock=time.time):
    """Yield (ready, retry_seconds); stamp only successfully completed work.

    State changes bypass the cooldown. Contention never waits while holding a
    database connection. A crash releases the OS lock without recording success.
    """
    root = Path(directory or tempfile.gettempdir())
    digest = hashlib.sha256(key.encode()).hexdigest()
    path = root / ("swt-background-" + digest + ".lock")
    try:
        stream = path.open("a+")
    except OSError:
        # Optional coordination must never disable alarms/config processing
        # when the host denies access to its temporary directory.
        with _local_guard:
            warn = str(root) not in _warned_paths
            _warned_paths.add(str(root))
        if warn:
            logging.getLogger(__name__).warning("Background lease directory unavailable; using process-local coordination: %s", root)
        with _local_lease(key, signature, interval, clock) as claim:
            yield claim
        return
    with stream:
        if stream.tell() == 0:
            stream.write(" ")
            stream.flush()
        stream.seek(0)
        locked = False
        try:
            try:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                yield False, 0.5
                return
            stream.seek(0)
            try:
                previous = json.load(stream)
            except (ValueError, TypeError):
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            now = clock()
            try:
                elapsed = now - float(previous.get("completed_at") or 0)
            except (TypeError, ValueError):
                elapsed = float("inf")
            if previous.get("signature") == signature and 0 <= elapsed < interval:
                yield False, interval - elapsed
                return
            yield True, 0
            stream.seek(0)
            stream.truncate()
            json.dump({"completed_at": clock(), "signature": signature}, stream)
            stream.flush()
        finally:
            if locked:
                stream.seek(0)
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                else:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
