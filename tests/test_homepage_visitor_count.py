from flask_app import server


def test_homepage_visits_increment_atomically_and_are_not_cached():
    original_value = server.get_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, "0")
    try:
        server.set_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, "2524")
        client = server.app.test_client()

        first = client.get("/")
        second = client.get("/homepage")

        assert first.status_code == 200
        assert second.status_code == 200
        assert b"2,525" in first.data
        assert b"2,526" in second.data
        assert first.headers["Cache-Control"] == "no-store"
        assert second.headers["Cache-Control"] == "no-store"
        assert server.get_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING) == "2526"
    finally:
        server.set_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, original_value)


def test_homepage_visit_recovers_from_an_invalid_saved_value():
    original_value = server.get_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, "0")
    try:
        server.set_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, "not-a-number")

        response = server.app.test_client().get("/")

        assert response.status_code == 200
        assert server.get_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING) == "1"
    finally:
        server.set_app_setting(server.HOMEPAGE_VISITOR_COUNT_SETTING, original_value)
