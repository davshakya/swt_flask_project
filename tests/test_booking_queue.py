import ast
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, redirect, request, url_for

from flask_app.booking_queue import BookingQueue


def test_concurrent_duplicate_bookings_are_saved_once(tmp_path):
    path = tmp_path / "queue.sqlite3"
    # Separate instances represent independent Passenger workers.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: BookingQueue(path).enqueue({"email": "a@example.com"}, {}), range(8)))
    assert len({booking_id for booking_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert BookingQueue(path).enqueue({"email": "b@example.com"}, {})[1]


def test_pending_work_survives_restart_and_completed_channels_are_not_resent(tmp_path):
    path = tmp_path / "queue.sqlite3"
    BookingQueue(path).enqueue({"name": "Customer"}, {})
    calls = []
    handlers = {"email": lambda cleaned, metadata: calls.append(cleaned) or True}
    queue = BookingQueue(path)
    queue.process(handlers, logging.getLogger(__name__))
    BookingQueue(path).process(handlers, logging.getLogger(__name__))
    assert calls == [{"name": "Customer"}]


def test_submission_token_prevents_late_retry_but_allows_new_booking(tmp_path, monkeypatch):
    queue = BookingQueue(tmp_path / "queue.sqlite3")
    monkeypatch.setattr("flask_app.booking_queue.time.time", lambda: 1000)
    first, _ = queue.enqueue({"name": "Customer"}, {"booking_token": "token-one"})
    monkeypatch.setattr("flask_app.booking_queue.time.time", lambda: 2000)
    assert queue.enqueue({"name": "Customer"}, {"booking_token": "token-one"}) == (first, False)
    assert queue.enqueue({"name": "Customer"}, {"booking_token": "token-two"})[1]


def test_interrupted_delivery_is_not_resent_but_other_channels_continue(tmp_path):
    queue = BookingQueue(tmp_path / "queue.sqlite3")
    booking_id, _ = queue.enqueue({}, {})
    queue.save_states(booking_id, {"email": "sending"})
    calls = []
    queue.process({"email": lambda *_: calls.append("email"),
                   "audit": lambda *_: calls.append("audit") or True}, logging.getLogger(__name__))
    assert calls == ["audit"]
    with queue.connect() as db:
        states = json.loads(db.execute("SELECT states FROM bookings").fetchone()[0])
    assert states == {"email": "review", "audit": "sent", "complete": True}


def test_booking_request_persists_and_redirects_without_mysql_or_notifications(tmp_path):
    # Execute the production route without importing server boot-time MySQL work.
    source = (Path(__file__).parents[1] / "flask_app" / "server.py").read_text(encoding="utf-8")
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "sales_enquiry")
    node.decorator_list = []
    app = Flask(__name__)
    app.add_url_rule("/", endpoint="dashboard", view_func=lambda: "ok")
    app.add_url_rule("/pricing", endpoint="pricing_page", view_func=lambda: "ok")
    queue = BookingQueue(tmp_path / "queue.sqlite3")
    scope = dict(request=request, redirect=redirect, url_for=url_for, jsonify=jsonify,
                 resolve_next_url=lambda value: value, dashboard_home_url=lambda _: "/",
                 validate_sales_enquiry_payload=lambda _: ({"name": "Customer"}, []),
                 sales_booking_queue=queue, start_sales_booking_worker=lambda: None,
                 sales_booking_wake=SimpleNamespace(set=lambda: None), logger=logging.getLogger(__name__))
    exec(compile(ast.Module(body=[node], type_ignores=[]), "sales_enquiry", "exec"), scope)
    with app.test_request_context("/sales/enquiry", method="POST", data={"return_to": "homepage"}):
        assert scope["sales_enquiry"]().location == "/?enquiry=success"
        assert scope["sales_enquiry"]().location == "/?enquiry=success"
    with app.test_request_context("/sales/enquiry", method="POST", headers={"Accept": "application/json"}, data={"return_to": "homepage"}):
        assert scope["sales_enquiry"]().get_json() == {"ok": True, "booking_id": 1}
    with queue.connect() as db:
        assert db.execute("SELECT count(*) FROM bookings").fetchone()[0] == 1
    def unavailable(*_):
        raise OSError("disk unavailable")
    scope["sales_booking_queue"] = SimpleNamespace(enqueue=unavailable)
    with app.test_request_context("/sales/enquiry", method="POST", data={"return_to": "homepage"}):
        assert scope["sales_enquiry"]()[1] == 503
    with app.test_request_context("/sales/enquiry", method="POST", headers={"Accept": "application/json"}, data={"return_to": "homepage"}):
        response, status = scope["sales_enquiry"]()
        assert status == 503
        assert response.get_json()["ok"] is False
    scope["validate_sales_enquiry_payload"] = lambda _: ({}, ["Please enter your name."])
    with app.test_request_context("/sales/enquiry", method="POST", headers={"Accept": "application/json"}, data={"return_to": "homepage"}):
        response, status = scope["sales_enquiry"]()
        assert status == 400
        assert response.get_json() == {"ok": False, "error": "Please enter your name."}


def test_ambiguous_email_failure_is_not_automatically_resent(tmp_path):
    queue = BookingQueue(tmp_path / "queue.sqlite3")
    booking_id, _ = queue.enqueue({}, {})
    def failing(*_):
        raise RuntimeError("connection lost after SMTP acceptance")
    queue.process({"email": failing}, logging.getLogger(__name__))
    queue.process({"email": lambda *_: (_ for _ in ()).throw(AssertionError("resent"))}, logging.getLogger(__name__))
    with queue.connect() as db:
        assert json.loads(db.execute("SELECT states FROM bookings WHERE id=?", (booking_id,)).fetchone()[0])["email"] == "review"


def test_two_notification_workers_cannot_send_same_booking(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = BookingQueue(path)
    queue.enqueue({}, {})
    entered, release = threading.Event(), threading.Event()
    calls = []
    def send(*_):
        calls.append("sent")
        entered.set()
        assert release.wait(5)
        return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(queue.process, {"email": send}, logging.getLogger(__name__))
        assert entered.wait(5)
        try:
            BookingQueue(path).process({"email": send}, logging.getLogger(__name__))
        finally:
            release.set()
        first.result()
    assert calls == ["sent"]
