from flask_app import server


def test_homepage_is_database_free_and_is_not_http_cached(monkeypatch):
    with server.homepage_visitor_count_lock:
        server.homepage_visitor_count_cached = 2524
        server.homepage_visitor_count_pending = 0
        server.homepage_visitor_count_worker_running = False
    monkeypatch.setattr(
        server,
        "increment_homepage_visitor_count",
        lambda: (_ for _ in ()).throw(AssertionError("homepage attempted visitor DB update")),
    )
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
    assert b"2,524" in first.data
    assert b"2,524" in second.data
    assert first.headers["Cache-Control"] == "no-store"
    assert second.headers["Cache-Control"] == "no-store"
