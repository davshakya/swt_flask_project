import base64
import json
import sqlite3
import sys
from pathlib import Path


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/tank.db")
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        customer_accounts = [
            {
                "device_id": row["device_id"],
                "display_name": row["display_name"],
                "password_hash": row["password_hash"],
                "active": int(row["active"] or 0),
            }
            for row in db.execute(
                """
                SELECT device_id, display_name, password_hash, active
                FROM customer_accounts
                ORDER BY device_id ASC
                """
            ).fetchall()
        ]
        app_settings = {
            row["key"]: row["value"]
            for row in db.execute(
                """
                SELECT key, value
                FROM app_settings
                WHERE key IN ('app_secret_key', 'dashboard_password')
                """
            ).fetchall()
        }

    customer_bootstrap = base64.b64encode(
        json.dumps({"accounts": customer_accounts}, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    print(f"APP_SECRET_KEY={app_settings.get('app_secret_key', '')}")
    print(f"DASHBOARD_PASSWORD_HASH={app_settings.get('dashboard_password', '')}")
    print(f"CUSTOMER_ACCOUNTS_BOOTSTRAP_B64={customer_bootstrap}")


if __name__ == "__main__":
    main()
