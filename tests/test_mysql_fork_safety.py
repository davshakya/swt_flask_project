from flask_app.capacity_db_pool import MySqlConnectionPool
from flask_app.mysql_circuit import MySqlConnectionCircuit


class Socket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self._sock = Socket()
        self.protocol_calls = 0

    def rollback(self):
        self.protocol_calls += 1

    def close(self):
        self.protocol_calls += 1


def test_child_detaches_idle_and_checked_out_sockets_without_mysql_commands():
    pool = MySqlConnectionPool(Connection)
    idle, idle_at = pool.acquire()
    leased, leased_at = pool.acquire()
    sockets = [idle._sock, leased._sock]
    pool.release(idle, idle_at)
    idle.protocol_calls = 0
    pool._after_fork()
    assert all(sock.closed for sock in sockets)
    assert idle._sock is leased._sock is None
    pool.release(leased, leased_at)
    assert idle.protocol_calls == leased.protocol_calls == 0
    assert pool.stats()['total'] == pool.stats()['idle'] == 0
    fresh, fresh_at = pool.acquire()
    assert fresh is not leased and fresh is not idle
    pool.release(fresh, fresh_at)
    assert pool.stats()['total'] == 1


def test_child_recovers_when_parent_circuit_lock_and_probe_were_active():
    circuit = MySqlConnectionCircuit()
    parent_lock = circuit._lock
    parent_lock.acquire()
    circuit._probe = True
    circuit._retry_at = float('inf')
    try:
        circuit._after_fork()
        assert circuit._lock is not parent_lock
        assert circuit._lock.acquire(blocking=False)
        circuit._lock.release()
        assert circuit.run(lambda: 'connected', lambda exc: True) == 'connected'
    finally:
        parent_lock.release()
