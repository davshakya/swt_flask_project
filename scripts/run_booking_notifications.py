#!/usr/bin/env python3
"""Inspect or drain durable booking notifications outside web workers."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from flask_app.booking_queue import BookingQueue
from flask_app.runtime_utils import parse_simple_dotenv


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drain", action="store_true", help="Deliver pending channels; default only inspects")
    parser.add_argument("--queue", type=Path)
    args = parser.parse_args(argv)
    os.chdir(PROJECT_ROOT)
    for key, value in parse_simple_dotenv(PROJECT_ROOT / "device.env").items():
        os.environ.setdefault(key, value)
    backup = Path(os.environ.get("SALES_ENQUIRY_BACKUP_PATH", str(PROJECT_ROOT / "data/sales_enquiries.jsonl")))
    queue = BookingQueue(args.queue or backup.with_suffix(".queue.sqlite3"))
    if args.drain:
        # Load the existing notification configuration and handlers, but skip
        # MySQL initialization, web background workers, MQTT and reconciler boot.
        os.environ["SWT_BOOKING_WORKER_ONLY"] = "true"
        from flask_app import server
        queue.process(server.sales_booking_handlers(), server.logger)
    status = queue.status()
    output = queue.path.with_suffix(".status.json")
    output.write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps({"status_file": str(output), "bookings": status}, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(run())
