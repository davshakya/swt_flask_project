from __future__ import annotations

import uuid

import pytest

from server import LOGIN_USERNAME, get_db


playwright_sync = pytest.importorskip(
    "playwright.sync_api",
    reason="Install Playwright to run browser UI tests.",
)
expect = playwright_sync.expect

pytestmark = pytest.mark.ui


def delete_sales_enquiry_records(unique_name):
    with get_db() as db:
        db.execute(
            "DELETE FROM ops_audit_log WHERE actor = ? AND action = ? AND details LIKE ?",
            ("public-lead", "sales_enquiry_submitted", f"%{unique_name}%"),
        )


def test_landing_page_login_modal_switches_roles(page, live_server_url):
    page.goto(f"{live_server_url}/", wait_until="domcontentloaded")
    page.wait_for_function("typeof window.openLoginModal === 'function'")

    page.locator("[data-testid='open-admin-login']").click()

    modal = page.locator("[data-testid='login-modal']")
    expect(modal).to_be_visible()
    expect(page.locator("#loginModalTitle")).to_have_text("Admin Login")
    expect(page.locator("#admin_username")).to_be_visible()

    modal.get_by_role("button", name="Customer Login", exact=True).click()

    expect(page.locator("#loginModalTitle")).to_have_text("Customer Login")
    expect(page.locator("#customer_username")).to_be_visible()


def test_admin_can_login_from_landing_modal(page, live_server_url, known_admin_password):
    page.goto(f"{live_server_url}/", wait_until="domcontentloaded")
    page.wait_for_function("typeof window.openLoginModal === 'function'")
    page.locator("[data-testid='open-admin-login']").click()
    expect(page.locator("[data-testid='login-modal']")).to_be_visible()

    page.locator("#admin_username").fill(LOGIN_USERNAME)
    page.locator("#admin_password").fill(known_admin_password)
    page.locator("[data-testid='admin-login-form']").evaluate("form => form.requestSubmit()")

    page.wait_for_url("**/admin/customers")
    expect(page.get_by_role("heading", name="Admin Dashboard")).to_be_visible()
    expect(page.get_by_role("link", name="Admin Password")).to_be_visible()


def test_customer_can_login_and_reach_dashboard(page, live_server_url, customer_test_account):
    page.goto(f"{live_server_url}/", wait_until="domcontentloaded")
    page.wait_for_function("typeof window.openLoginModal === 'function'")
    page.locator("[data-testid='open-customer-login']").click()
    expect(page.locator("[data-testid='login-modal']")).to_be_visible()

    page.locator("#customer_username").fill(customer_test_account["device_id"])
    page.locator("#customer_password").fill(customer_test_account["password"])
    page.locator("[data-testid='customer-login-form']").evaluate("form => form.requestSubmit()")

    page.wait_for_url("**/customer/dashboard")
    expect(page.get_by_role("heading", name="Home Water Dashboard")).to_be_visible()
    expect(page.get_by_text(customer_test_account["display_name"])).to_be_visible()


def test_sales_enquiry_form_shows_validation_error(page, live_server_url):
    page.goto(f"{live_server_url}/", wait_until="domcontentloaded")

    page.locator("#lead_name").fill("Playwright Lead")
    page.locator("#lead_phone").fill("123")
    page.locator("#lead_city").fill("Pune")
    page.locator("#lead_segment").select_option("Apartment / Hostel")
    page.locator("#lead_device_count").fill("6")
    page.locator("#lead_message").fill("Need a walkthrough for a hostel deployment.")
    page.locator("[data-testid='sales-enquiry-form']").get_by_role(
        "button",
        name="Book Installation Call",
    ).click()

    expect(page.get_by_text("Please enter a valid phone or WhatsApp number.")).to_be_visible()
    expect(page.locator("#lead_phone")).to_have_value("123")


def test_sales_enquiry_form_submits_successfully(page, live_server_url):
    unique_name = f"Playwright Lead {uuid.uuid4().hex[:8]}"
    delete_sales_enquiry_records(unique_name)

    try:
        page.goto(f"{live_server_url}/", wait_until="domcontentloaded")

        page.locator("#lead_name").fill(unique_name)
        page.locator("#lead_phone").fill("+91 9876543210")
        page.locator("#lead_city").fill("Pune")
        page.locator("#lead_segment").select_option("Apartment / Hostel")
        page.locator("#lead_device_count").fill("6")
        page.locator("#lead_message").fill("Need a pricing call for a hostel deployment.")
        page.locator("[data-testid='sales-enquiry-form']").get_by_role(
            "button",
            name="Book Installation Call",
        ).click()

        page.wait_for_url("**/?enquiry=success")
        expect(
            page.get_by_text(
                "Thanks for your enquiry. Our team can now follow up with pricing, installation guidance, or a demo."
            )
        ).to_be_visible()
    finally:
        delete_sales_enquiry_records(unique_name)
