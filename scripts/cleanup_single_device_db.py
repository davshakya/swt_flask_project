import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


DEVICE_TABLE_RULES = (
    ("tank_data", "device_id", False),
    ("customer_accounts", "device_id", False),
    ("registered_devices", "device_id", False),
    ("ops_alerts", "device_id", True),
    ("ops_audit_log", "device_id", True),
    ("device_command_queue", "target_device", False),
)


def table_exists(db, table_name):
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def backup_database(db_path):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = db_path.suffix or ".db"
    backup_name = f"{db_path.stem}.backup-before-single-device-{timestamp}{suffix}"
    backup_path = db_path.with_name(backup_name)
    shutil.copy2(db_path, backup_path)
    return backup_path


def delete_rows_for_other_devices(db, table_name, column_name, keep_device_id, keep_global_rows):
    if keep_global_rows:
        query = f"DELETE FROM {table_name} WHERE COALESCE({column_name}, '') NOT IN ('', ?)"
    else:
        query = f"DELETE FROM {table_name} WHERE COALESCE({column_name}, '') != ?"
    cursor = db.execute(query, (keep_device_id,))
    return cursor.rowcount


def delete_relay_queue_for_other_devices(db, keep_device_id, keep_global_rows):
    if not table_exists(db, "relay_queue"):
        return 0

    rows = db.execute("SELECT id, payload FROM relay_queue ORDER BY id ASC").fetchall()
    ids_to_delete = []

    for row in rows:
        payload = row["payload"]
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, dict):
            continue

        device_id = str(decoded.get("device_id") or "").strip()
        if not device_id and keep_global_rows:
            continue
        if device_id and device_id != keep_device_id:
            ids_to_delete.append((row["id"],))

    if not ids_to_delete:
        return 0

    db.executemany("DELETE FROM relay_queue WHERE id = ?", ids_to_delete)
    return len(ids_to_delete)


def collect_counts(db):
    counts = {}
    for table_name, column_name, _keep_global_rows in DEVICE_TABLE_RULES:
        if not table_exists(db, table_name):
            continue
        rows = db.execute(
            f"""
            SELECT COALESCE({column_name}, '<null>') AS value, COUNT(*) AS count
            FROM {table_name}
            GROUP BY COALESCE({column_name}, '<null>')
            ORDER BY count DESC, value ASC
            """
        ).fetchall()
        counts[table_name] = [(row["value"], row["count"]) for row in rows]
    if table_exists(db, "relay_queue"):
        counts["relay_queue"] = [("rows", db.execute("SELECT COUNT(*) FROM relay_queue").fetchone()[0])]
    return counts


def parse_args():
    parser = argparse.ArgumentParser(
        description="Delete all device-scoped rows except one device_id from the Flask SQLite database."
    )
    parser.add_argument(
        "--db-path",
        default="data/tank.db",
        help="Path to the SQLite database. Use /var/data/tank.db on Render persistent disk.",
    )
    parser.add_argument(
        "--keep-device-id",
        required=True,
        help="The exact device_id to keep, for example swt-000-000-000-001.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a timestamped backup copy before deleting rows.",
    )
    parser.add_argument(
        "--drop-global-rows",
        action="store_true",
        help="Also delete rows with empty/null device_id in ops_alerts, ops_audit_log, and relay_queue.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show current counts and exit without changing the database.",
    )
    parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip VACUUM after cleanup.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db_path).expanduser()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    keep_device_id = str(args.keep_device_id or "").strip()
    if not keep_device_id:
        raise SystemExit("--keep-device-id is required")

    if args.dry_run:
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            counts = collect_counts(db)
        print(f"db_path={db_path}")
        print(f"keep_device_id={keep_device_id}")
        for table_name, rows in counts.items():
            print(f"[{table_name}]")
            for value, count in rows:
                print(f"{value}\t{count}")
        return

    backup_path = None
    if not args.no_backup:
        backup_path = backup_database(db_path)

    summary = {}
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        for table_name, column_name, keep_global_rows in DEVICE_TABLE_RULES:
            if not table_exists(db, table_name):
                continue
            deleted_rows = delete_rows_for_other_devices(
                db,
                table_name,
                column_name,
                keep_device_id,
                keep_global_rows=keep_global_rows and not args.drop_global_rows,
            )
            summary[table_name] = deleted_rows

        summary["relay_queue"] = delete_relay_queue_for_other_devices(
            db,
            keep_device_id,
            keep_global_rows=not args.drop_global_rows,
        )
        db.commit()

        if not args.no_vacuum:
            db.execute("VACUUM")

        counts = collect_counts(db)

    print(f"db_path={db_path}")
    if backup_path:
        print(f"backup_path={backup_path}")
    print(f"keep_device_id={keep_device_id}")
    for table_name, deleted_rows in summary.items():
        print(f"deleted[{table_name}]={deleted_rows}")
    for table_name, rows in counts.items():
        print(f"[remaining:{table_name}]")
        for value, count in rows:
            print(f"{value}\t{count}")


if __name__ == "__main__":
    main()
