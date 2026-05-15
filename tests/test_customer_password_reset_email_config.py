from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"
ENV_EXAMPLE = PROJECT_ROOT / "flask_app" / ".env.example"


def test_customer_forgot_password_requires_smtp_before_claiming_email_sent():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = source.index("def customer_forgot_password():")
    route_source = source[route_start : source.index("\n\n@app.route(\"/login/customer/reset-password", route_start)]

    assert "if not SMTP_HOST:" in route_source
    assert "Password reset email is not configured on this server." in route_source
    assert route_source.index("if not SMTP_HOST:") < route_source.index("success = \"If that customer account has an email on file")


def test_env_example_documents_customer_password_reset_smtp_settings():
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")

    for key in (
        "CUSTOMER_COMMUNICATION_FROM_EMAIL=",
        "CUSTOMER_COMMUNICATION_FROM_NAME=",
        "SMTP_HOST=mail.salewell.co.in",
        "SMTP_PORT=465",
        "SMTP_USERNAME=support@salewell.co.in",
        "SMTP_PASSWORD=",
        "SMTP_USE_TLS=false",
        "SMTP_USE_SSL=true",
    ):
        assert key in env_text


def test_customer_password_reset_sender_defaults_to_support_mailbox():
    source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert 'CUSTOMER_COMMUNICATION_FROM_EMAIL = os.environ.get("CUSTOMER_COMMUNICATION_FROM_EMAIL", "support@salewell.co.in").strip()' in source
    assert "message[\"From\"] = from_header" in source


def test_customer_email_sender_supports_cpanel_ssl_smtp():
    source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert 'SMTP_USE_SSL = env_flag("SMTP_USE_SSL", default=False)' in source
    assert "smtp_client = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP" in source
    assert "if SMTP_USE_TLS and not SMTP_USE_SSL:" in source
