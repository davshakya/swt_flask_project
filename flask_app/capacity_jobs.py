from __future__ import annotations

import json


def enqueue_job(cursor, job_kind, payload, device_id=None):
    cursor.execute(
        """
        INSERT INTO background_jobs(job_kind, device_id, payload_json, status, available_at)
        VALUES (?, ?, ?, 'queued', CURRENT_TIMESTAMP)
        """,
        (job_kind, device_id, json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)),
    )
    return int(cursor.lastrowid)


def claim_jobs(cursor, batch_size=25):
    limit = max(1, min(int(batch_size), 100))
    cursor.execute(
        """
        UPDATE background_jobs
        SET status='queued', locked_at=NULL, available_at=CURRENT_TIMESTAMP,
            last_error=COALESCE(last_error, 'stale worker lease recovered')
        WHERE status='running' AND locked_at < datetime('now', '-900 seconds')
        """
    )
    rows = cursor.execute(
        """
        SELECT id, job_kind, device_id, payload_json, attempt_count
        FROM background_jobs
        WHERE status='queued' AND available_at <= CURRENT_TIMESTAMP
        ORDER BY available_at, id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    claimed = []
    for row in rows:
        cursor.execute(
            """
            UPDATE background_jobs
            SET status='running', locked_at=CURRENT_TIMESTAMP,
                attempt_count=attempt_count + 1, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='queued'
            """,
            (row["id"],),
        )
        if int(cursor.rowcount or 0) == 1:
            item = dict(row)
            item["attempt_count"] = int(item.get("attempt_count") or 0) + 1
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            claimed.append(item)
    return claimed


def complete_job(cursor, job_id):
    cursor.execute(
        """
        UPDATE background_jobs
        SET status='completed', completed_at=CURRENT_TIMESTAMP, locked_at=NULL,
            last_error=NULL, updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND status='running'
        """,
        (job_id,),
    )


def fail_job(cursor, job_id, error, attempt_count, max_attempts=5):
    terminal = int(attempt_count) >= max(1, int(max_attempts))
    status = "failed" if terminal else "queued"
    delay_seconds = min(3600, 30 * (2 ** max(0, int(attempt_count) - 1)))
    cursor.execute(
        """
        UPDATE background_jobs
        SET status=?, locked_at=NULL, last_error=?,
            available_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND status='running'
        """,
        (status, str(error)[:2000], f"+{delay_seconds} seconds", job_id),
    )
    return status


def process_job_batch(get_db, handlers, batch_size=25, max_attempts=5):
    with get_db() as db:
        jobs = claim_jobs(db.cursor(), batch_size=batch_size)
    result = {"claimed": len(jobs), "completed": 0, "retried": 0, "failed": 0}
    for job in jobs:
        try:
            handler = handlers[job["job_kind"]]
            handler(job["payload"])
        except Exception as exc:
            with get_db() as db:
                status = fail_job(db.cursor(), job["id"], exc, job["attempt_count"], max_attempts)
            result["failed" if status == "failed" else "retried"] += 1
        else:
            with get_db() as db:
                complete_job(db.cursor(), job["id"])
            result["completed"] += 1
    return result
