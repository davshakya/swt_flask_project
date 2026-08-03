from flask_app import server
import time


def test_homepage_visits_increment_asynchronously_and_are_not_http_cached():
    original_value = server.get_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, "0")
    try:
        server.set_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, "2524")
        with server.homepage_visitor_count_lock:
            server.homepage_visitor_count_cached = 2524
            server.homepage_visitor_count_pending = 0
            server.homepage_visitor_count_worker_running = False
        client = server.app.test_client()

        first = client.get("/")
        second = client.get("/homepage")

        assert first.status_code == 200
        assert second.status_code == 200
        assert b"2,525" in first.data
        assert b"2,526" in second.data
        assert first.headers["Cache-Control"] == "no-store"
        assert second.headers["Cache-Control"] == "no-store"
        deadline = time.time() + 2
        while time.time() < deadline and server.get_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING) != "2526":
            time.sleep(0.01)
        assert server.get_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING) == "2526"
    finally:
        server.set_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, original_value)
