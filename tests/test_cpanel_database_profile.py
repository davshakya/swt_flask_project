from flask_app import server


def test_database_url_decodes_percent_encoded_cpanel_credentials(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "mysql+pymysql://sale%2Buser:p%40ss%2Fword@localhost:3306/sale_db",
    )

    config = server.mysql_connection_config()

    assert config == {
        "host": "localhost",
        "port": 3306,
        "user": "sale+user",
        "password": "p@ss/word",
        "database": "sale_db",
    }


def test_cpanel_profile_disables_database_creation(monkeypatch):
    monkeypatch.setenv("MYSQL_AUTO_CREATE_DATABASE", "false")

    assert server.mysql_auto_create_database_enabled() is False


def test_mysql_lock_wait_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("MYSQL_LOCK_WAIT_TIMEOUT_SECONDS", "999")
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(server, "mysql_connection_config", lambda: {
        "host": "localhost",
        "port": 3306,
        "user": "sale_user",
        "password": "secret",
        "database": "sale_db",
    })
    monkeypatch.setattr(server.pymysql, "connect", lambda **_kwargs: FakeConnection())

    server.connect_mysql()

    assert calls[-1] == (
        "SET SESSION innodb_lock_wait_timeout = %s",
        (60,),
    )
