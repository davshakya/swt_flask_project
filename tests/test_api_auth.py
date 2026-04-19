from __future__ import annotations

import pytest

from server import LOGIN_USERNAME, MOBILE_TOKEN_MAX_AGE_SECONDS, app


pytestmark = pytest.mark.api


def mobile_login(client, username, password):
    return client.post(
        "/api/mobile/auth/login",
        json={"username": username, "password": password},
    )


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_mobile_auth_login_returns_admin_token_and_viewer(known_admin_password):
    response = mobile_login(app.test_client(), LOGIN_USERNAME, known_admin_password)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["token"]
    assert payload["viewer"]["role"] == "admin"
    assert payload["viewer"]["username"] == LOGIN_USERNAME
    assert payload["viewer"]["device_id"] is None
    assert payload["viewer"]["display_name"] == "Administrator"
    assert payload["expires_in_seconds"] == MOBILE_TOKEN_MAX_AGE_SECONDS


def test_mobile_auth_login_rejects_invalid_credentials():
    response = mobile_login(app.test_client(), LOGIN_USERNAME, "definitely-wrong-password")

    assert response.status_code == 401
    assert response.get_json() == {"error": "invalid username or password"}


def test_mobile_bootstrap_allows_admin_to_scope_selected_device(known_admin_password, customer_test_account):
    client = app.test_client()
    login_response = mobile_login(client, LOGIN_USERNAME, known_admin_password)
    token = login_response.get_json()["token"]

    response = client.get(
        f"/api/mobile/bootstrap?device_id={customer_test_account['device_id']}",
        headers=auth_headers(token),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["viewer"]["role"] == "admin"
    assert payload["snapshot"]["device_id"] == customer_test_account["device_id"]
    assert payload["service_config"]["device_id"] == customer_test_account["device_id"]
    assert "ops" in payload


def test_mobile_bootstrap_scopes_customer_to_their_own_device(customer_test_account):
    client = app.test_client()
    login_response = mobile_login(
        client,
        customer_test_account["device_id"],
        customer_test_account["password"],
    )
    token = login_response.get_json()["token"]

    own_response = client.get("/api/mobile/bootstrap", headers=auth_headers(token))
    assert own_response.status_code == 200
    own_payload = own_response.get_json()
    assert own_payload["viewer"]["role"] == "customer"
    assert own_payload["viewer"]["device_id"] == customer_test_account["device_id"]
    assert own_payload["snapshot"]["device_id"] == customer_test_account["device_id"]
    assert "ops" not in own_payload

    other_response = client.get(
        "/api/mobile/bootstrap?device_id=swt-someone-else-001",
        headers=auth_headers(token),
    )
    assert other_response.status_code == 403


def test_mobile_bootstrap_requires_authentication():
    response = app.test_client().get("/api/mobile/bootstrap")

    assert response.status_code == 401
    assert response.get_json() == {"error": "authentication required"}
