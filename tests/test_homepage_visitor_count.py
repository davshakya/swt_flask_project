from flask_app import server


def test_homepage_increments_cached_visitor_count_and_is_not_http_cached(monkeypatch):
    with server.homepage_visitor_count_lock:
        server.homepage_visitor_count_cached = 2524
        server.homepage_visitor_count_pending = 0
        server.homepage_visitor_count_worker_running = False
    def increment_cached_count():
        with server.homepage_visitor_count_lock:
            server.homepage_visitor_count_cached += 1
            return server.homepage_visitor_count_cached

    monkeypatch.setattr(server, "increment_homepage_visitor_count", increment_cached_count)
    monkeypatch.setattr(
        server,
        "get_app_setting",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("homepage attempted DB read")),
    )
    client = server.app.test_client()

    first = client.get("/")
    second = client.get("/homepage")

    assert first.status_code == 200
    assert second.status_code == 200
    assert b"2,525" in first.data
    assert b"2,526" in second.data
    assert first.headers["Cache-Control"] == "no-store"
    assert second.headers["Cache-Control"] == "no-store"


def test_homepage_visitor_count_restores_persisted_value_and_increments(monkeypatch):
    with server.homepage_visitor_count_lock:
        server.homepage_visitor_count_cached = None
        server.homepage_visitor_count_pending = 0
        server.homepage_visitor_count_worker_running = False

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            if sql.lstrip().startswith("SELECT"):
                return type("Result", (), {"fetchone": lambda self: {"value": "2525"}})()
            return self

    monkeypatch.setattr(server, "get_db", FakeDb)

    count = server.increment_homepage_visitor_count()

    assert count == 2525
    assert server.homepage_visitor_count_cached == 2525
    assert server.homepage_visitor_count_pending == 0


def test_homepage_renders_atomic_database_count_instead_of_stale_worker_cache(monkeypatch):
    with server.homepage_visitor_count_lock:
        server.homepage_visitor_count_cached = 100

    class FakeDb:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            if sql.lstrip().startswith("SELECT"):
                return type("Result", (), {"fetchone": lambda self: {"value": "3290"}})()
            return self

    monkeypatch.setattr(server, "get_db", FakeDb)

    response = server.app.test_client().get("/")

    assert response.status_code == 200
    assert b"3,290" in response.data
    assert server.homepage_visitor_count_cached == 3290
