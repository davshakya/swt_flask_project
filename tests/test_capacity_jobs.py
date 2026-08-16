from __future__ import annotations

from types import SimpleNamespace

from flask_app import capacity_jobs


class Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.rowcount = 1
        self.lastrowid = 42

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


def test_enqueue_serializes_payload_and_returns_job_id():
    cursor = Cursor()
    job_id = capacity_jobs.enqueue_job(cursor, "alert_webhook", {"severity": "critical"}, "swt-1")
    assert job_id == 42
    assert cursor.calls[0][1][0:2] == ("alert_webhook", "swt-1")
    assert cursor.calls[0][1][2] == '{"severity":"critical"}'


def test_claim_is_bounded_and_moves_job_to_running():
    cursor = Cursor(
        [{"id": 7, "job_kind": "alert_webhook", "device_id": "swt-1", "payload_json": '{"x":1}', "attempt_count": 0}]
    )
    jobs = capacity_jobs.claim_jobs(cursor, batch_size=999)
    assert cursor.calls[1][1] == (100,)
    assert jobs[0]["payload"] == {"x": 1}
    assert jobs[0]["attempt_count"] == 1


def test_job_batch_retries_failure_and_completes_success(monkeypatch):
    jobs = [
        {"id": 1, "job_kind": "ok", "payload": {}, "attempt_count": 1},
        {"id": 2, "job_kind": "bad", "payload": {}, "attempt_count": 1},
    ]
    monkeypatch.setattr(capacity_jobs, "claim_jobs", lambda *_args, **_kwargs: jobs)
    completed = []
    failed = []
    monkeypatch.setattr(capacity_jobs, "complete_job", lambda _cursor, job_id: completed.append(job_id))
    monkeypatch.setattr(
        capacity_jobs,
        "fail_job",
        lambda _cursor, job_id, *_args, **_kwargs: failed.append(job_id) or "queued",
    )

    class Db:
        def __enter__(self):
            return SimpleNamespace(cursor=lambda: object())

        def __exit__(self, *_args):
            return False

    result = capacity_jobs.process_job_batch(
        lambda: Db(),
        {"ok": lambda _payload: None, "bad": lambda _payload: (_ for _ in ()).throw(RuntimeError("fail"))},
    )
    assert result == {"claimed": 2, "completed": 1, "retried": 1, "failed": 0}
    assert completed == [1]
    assert failed == [2]
