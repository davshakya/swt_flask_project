from types import SimpleNamespace
import stat

import pytest

from flask_app import mysql_transport


def test_discovery_requires_real_socket(monkeypatch):
    monkeypatch.setattr(mysql_transport, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(mysql_transport, "Path", lambda path: SimpleNamespace(stat=lambda: SimpleNamespace(
        st_mode=stat.S_IFSOCK if path == "/run/mysqld/mysqld.sock" else stat.S_IFREG)))
    assert mysql_transport.discover_local_mysql_socket(
        {"host": "localhost", "port": 3306}, {}) == "/run/mysqld/mysqld.sock"


@pytest.mark.parametrize("host,port,environ", [
    ("remote-db", 3306, {}), ("127.0.0.1", 3306, {}),
    ("localhost", 3307, {}), ("localhost", 3306, {"MYSQL_SSL_CA": "/ca.pem"}),
])
def test_discovery_never_redirects_other_instances_or_tls(monkeypatch, host, port, environ):
    monkeypatch.setattr(mysql_transport, "os", SimpleNamespace(name="posix"))
    def unexpected_stat(_path):
        raise AssertionError("discovery must not probe sockets")
    monkeypatch.setattr(mysql_transport, "Path", unexpected_stat)
    assert mysql_transport.discover_local_mysql_socket({"host": host, "port": port}, environ) is None


def test_missing_or_inaccessible_sockets_preserve_tcp(monkeypatch):
    monkeypatch.setattr(mysql_transport, "os", SimpleNamespace(name="posix"))
    def inaccessible():
        raise PermissionError("unavailable")
    monkeypatch.setattr(mysql_transport, "Path", lambda _path: SimpleNamespace(stat=inaccessible))
    assert mysql_transport.discover_local_mysql_socket({"host": "localhost", "port": 3306}, {}) is None


def test_windows_does_not_probe_unix_sockets(monkeypatch):
    monkeypatch.setattr(mysql_transport, "os", SimpleNamespace(name="nt"))
    assert mysql_transport.discover_local_mysql_socket({"host": "localhost", "port": 3306}, {}) is None
