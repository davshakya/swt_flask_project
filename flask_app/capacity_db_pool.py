from __future__ import annotations

from dataclasses import dataclass
import threading
import time


@dataclass
class _IdleConnection:
    connection: object
    created_at: float


class MySqlConnectionPool:
    """Small process-local pool designed for constrained shared hosting."""

    def __init__(self, factory, size=2, max_overflow=1, recycle_seconds=240, wait_seconds=5):
        self.factory = factory
        self.size = max(1, min(int(size), 4))
        self.max_overflow = max(0, min(int(max_overflow), 2))
        self.recycle_seconds = max(30, min(int(recycle_seconds), 900))
        self.wait_seconds = max(1, min(int(wait_seconds), 30))
        self._condition = threading.Condition()
        self._idle = []
        self._total = 0
        self._created = 0
        self._recycled = 0

    def _close(self, connection):
        try:
            connection.close()
        except Exception:
            pass

    def acquire(self):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            candidate = None
            with self._condition:
                if self._idle:
                    candidate = self._idle.pop()
                elif self._total < self.size + self.max_overflow:
                    self._total += 1
                    create = True
                    break
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("MySQL connection pool exhausted")
                    self._condition.wait(remaining)
                    continue
            age = time.monotonic() - candidate.created_at
            try:
                if age >= self.recycle_seconds:
                    raise RuntimeError("recycle")
                candidate.connection.ping(reconnect=False)
                return candidate.connection, candidate.created_at
            except Exception:
                self._close(candidate.connection)
                with self._condition:
                    self._total -= 1
                    self._recycled += 1
                    self._condition.notify()

        try:
            connection = self.factory()
        except Exception:
            with self._condition:
                self._total -= 1
                self._condition.notify()
            raise
        with self._condition:
            self._created += 1
        return connection, time.monotonic()

    def release(self, connection, created_at, discard=False):
        try:
            connection.rollback()
        except Exception:
            discard = True
        with self._condition:
            retain = not discard and len(self._idle) < self.size
            if retain:
                self._idle.append(_IdleConnection(connection, created_at))
            else:
                self._total -= 1
            self._condition.notify()
        if not retain:
            self._close(connection)

    def stats(self):
        with self._condition:
            return {
                "size": self.size,
                "max_overflow": self.max_overflow,
                "total": self._total,
                "idle": len(self._idle),
                "created": self._created,
                "recycled": self._recycled,
            }
