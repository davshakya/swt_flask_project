"""Bound connection attempts during a shared-host database outage."""
import threading
import os
import time


class MySqlConnectionCircuit:
    def __init__(self, failure_limit=3, cooldown_seconds=5, clock=time.monotonic):
        self.failure_limit = failure_limit
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._retry_at = 0.0
        self._probe = False
        self._generation = 0
        if hasattr(os, 'register_at_fork'):
            os.register_at_fork(after_in_child=self._after_fork)

    def _after_fork(self):
        # The parent's threads and in-flight probes do not exist in the child.
        self._lock = threading.Lock()
        self._failures = 0
        self._retry_at = 0.0
        self._probe = False
        self._generation = 0

    def run(self, operation, is_transient):
        with self._lock:
            if self._retry_at:
                if self.clock() < self._retry_at or self._probe:
                    raise TimeoutError('MySQL connection recovery cooldown; please retry')
                self._probe = True
            generation = self._generation
        try:
            result = operation()
        except Exception as exc:
            transient = is_transient(exc)
            with self._lock:
                if generation == self._generation:
                    self._probe = False
                    self._failures = self._failures + 1 if transient else 0
                    if self._failures >= self.failure_limit:
                        self._retry_at = self.clock() + self.cooldown_seconds
                        self._generation += 1
                    elif not transient:
                        self._retry_at = 0.0
            raise
        with self._lock:
            # A connection started before another request opened the circuit
            # must not clear that newer outage observation.
            if generation == self._generation:
                self._failures = 0
                self._retry_at = 0.0
                self._probe = False
        return result
