"""Durable booking intake independent of the telemetry MySQL database."""
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from flask_app.background_lease import background_lease


class BookingQueue:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=1)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL,
                created REAL NOT NULL, payload TEXT NOT NULL, states TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0, submission_key TEXT UNIQUE
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS booking_fingerprint ON bookings(fingerprint, created)")
            db.execute("CREATE INDEX IF NOT EXISTS booking_pending ON bookings(complete, id)")
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, cleaned, metadata):
        fingerprint = hashlib.sha256(json.dumps(cleaned, sort_keys=True).encode()).hexdigest()
        token = str(metadata.get("booking_token") or "")[:128]
        submission_key = hashlib.sha256((token + fingerprint).encode()).hexdigest() if token else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM bookings WHERE submission_key=? OR (fingerprint=? AND created>=?) ORDER BY id DESC LIMIT 1",
                (submission_key, fingerprint, time.time() - 600),
            ).fetchone()
            if existing:
                return existing[0], False
            cursor = db.execute(
                "INSERT INTO bookings(fingerprint,created,payload,states,submission_key) VALUES(?,?,?,?,?)",
                (fingerprint, time.time(), json.dumps([cleaned, metadata]), "{}", submission_key),
            )
            return cursor.lastrowid, True

    def process(self, handlers, logger):
        # Keep the OS lock throughout delivery; a worker crash releases it.
        # A persisted 'sending' state is intentionally never automatically resent:
        # SMTP may have accepted the message before the worker disappeared.
        with background_lease("bookings:" + str(self.path.resolve()), "", interval=0, directory=self.path.parent) as (ready, _):
            if not ready:
                return
            with self.connect() as db:
                rows = db.execute("SELECT id,payload,states FROM bookings WHERE complete=0 ORDER BY id LIMIT 20").fetchall()
            for booking_id, payload, raw_states in rows:
                cleaned, metadata = json.loads(payload)
                states = json.loads(raw_states)
                metadata["notification_states"] = states
                for channel, handler in handlers.items():
                    if channel in states:
                        if states[channel] == "sending":
                            logger.warning("Booking %s %s delivery interrupted; review before resending", booking_id, channel)
                            states[channel] = "review"
                        continue
                    states[channel] = "sending"
                    self.save_states(booking_id, states)
                    try:
                        states[channel] = "sent" if handler(cleaned, metadata) else "failed"
                    except Exception:
                        states[channel] = "review"
                        logger.exception("Booking %s %s failed; review before resending", booking_id, channel)
                    self.save_states(booking_id, states)
                    if states[channel] != "sent":
                        logger.warning("Booking %s %s status=%s", booking_id, channel, states[channel])
                states["complete"] = True
                self.save_states(booking_id, states)

    def save_states(self, booking_id, states):
        with self.connect() as db:
            db.execute("UPDATE bookings SET states=?,complete=? WHERE id=?", (json.dumps(states), int(states.get("complete", False)), booking_id))
