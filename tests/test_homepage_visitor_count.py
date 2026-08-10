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

    monkeypatch.setattr(server, "get_app_setting", lambda *args, **kwargs: "2524")
    monkeypatch.setattr(server, "threading", type("threading", (), {"Thread": lambda *args, **kwargs: type("DummyThread", (), {"start": lambda self: None})()}))

    count = server.increment_homepage_visitor_count()

    assert count == 2525
    assert server.homepage_visitor_count_cached == 2525
    assert server.homepage_visitor_count_pending == 1
