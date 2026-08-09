#!/usr/bin/env python3
"""Validate backup artifacts and record durable cPanel backup/DR evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_backup(path, max_age_hours=36):
    backup = Path(path).resolve(strict=True)
    if not backup.is_file() or backup.stat().st_size <= 0:
        raise ValueError("backup is empty")
    age_hours = (datetime.now(timezone.utc).timestamp() - backup.stat().st_mtime) / 3600
    if age_hours > max(1, int(max_age_hours)):
        raise ValueError(f"backup is stale ({age_hours:.1f} hours)")
    digest = sha256_file(backup)
    checksum_path = Path(f"{backup}.sha256")
    if checksum_path.exists():
        expected = checksum_path.read_text(encoding="utf-8").split()[0].strip().lower()
        if expected != digest:
            raise ValueError("backup checksum mismatch")
    if backup.suffix == ".gz":
        uncompressed_bytes = 0
        prefix = b""
        with gzip.open(backup, "rb") as archive:
            while True:
                chunk = archive.read(1024 * 1024)
                if not chunk:
                    break
                if len(prefix) < 65536:
                    prefix += chunk[: 65536 - len(prefix)]
                uncompressed_bytes += len(chunk)
        if uncompressed_bytes < 32 or not any(marker in prefix.upper() for marker in (b"MYSQL DUMP", b"CREATE TABLE", b"INSERT INTO")):
            raise ValueError("backup does not contain a non-empty SQL dump")
    else:
        uncompressed_bytes = backup.stat().st_size
    return {
        "path": str(backup),
        "bytes": backup.stat().st_size,
        "uncompressed_bytes": uncompressed_bytes,
        "sha256": digest,
        "age_hours": round(age_hours, 2),
    }


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--offserver-copy", type=Path)
    parser.add_argument("--max-age-hours", type=int, default=36)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(PROJECT_ROOT))
    from flask_app import server
    from flask_app.capacity_operations import record_runtime_status

    if not server.CAPACITY_FEATURES.enabled("offserver_backup"):
        print(json.dumps({"ok": True, "skipped": "feature_disabled"}))
        return 0
    try:
        details = {"local": validate_backup(args.backup, args.max_age_hours)}
        if args.offserver_copy:
            details["offserver"] = validate_backup(args.offserver_copy, args.max_age_hours)
            if details["local"]["sha256"] != details["offserver"]["sha256"]:
                raise ValueError("off-server copy does not match local backup")
    except Exception as exc:
        details = {"error": str(exc)}
        status = "failed"
    else:
        status = "ok"
    with server.get_db() as db:
        record_runtime_status(db.cursor(), "backup", status, details)
    print(json.dumps({"ok": status == "ok", "status": status, "details": details}, sort_keys=True))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(run())
