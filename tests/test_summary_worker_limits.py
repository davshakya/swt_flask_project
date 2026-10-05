"""Simulate shared-host thread pressure without a database or real threads."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


def scheduler(start_error=False, refresh_error=False):
    source = Path(__file__).resolve().parents[1] / 'flask_app' / 'server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'schedule_dashboard_summary_refresh')
    tasks, refreshed = [], []

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            if start_error:
                raise RuntimeError("can't start new thread")
            tasks.append(self.target)

    def refresh(device):
        refreshed.append(device)
        if refresh_error:
            raise RuntimeError('database offline')

    namespace = {
        'normalize_device_id': lambda device: device,
        'app': SimpleNamespace(testing=False),
        'threading': SimpleNamespace(Thread=Thread),
        'time': SimpleNamespace(sleep=lambda delay: None, monotonic=lambda: 100),
        'dashboard_summary_refresh_lock': threading.Lock(),
        'dashboard_summary_refresh_pending': set(),
        'dashboard_summary_last_refresh_at': {},
        'DASHBOARD_SUMMARY_MIN_REFRESH_SECONDS': 30,
        'telemetry_background_semaphore': threading.BoundedSemaphore(1),
        'refresh_dashboard_summary': refresh,
        'logger': SimpleNamespace(warning=lambda *args: None),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace, tasks, refreshed


def test_summary_burst_starts_only_available_workers_and_can_retry():
    namespace, tasks, refreshed = scheduler()
    schedule = namespace['schedule_dashboard_summary_refresh']
    schedule('first')
    for device in ['first', 'second', 'third']:
        schedule(device)
    assert len(tasks) == 1
    assert namespace['dashboard_summary_refresh_pending'] == {'first'}
    tasks.pop(0)()
    schedule('second')
    tasks.pop(0)()
    assert refreshed == ['first', 'second']
    assert not namespace['dashboard_summary_refresh_pending']


def test_summary_does_not_create_threads_while_telemetry_owns_permit():
    namespace, tasks, _ = scheduler()
    permit = namespace['telemetry_background_semaphore']
    assert permit.acquire(blocking=False)
    namespace['schedule_dashboard_summary_refresh']('device')
    assert not tasks
    assert not namespace['dashboard_summary_refresh_pending']
    permit.release()
    namespace['schedule_dashboard_summary_refresh']('device')
    assert len(tasks) == 1
    tasks[0]()


@pytest.mark.parametrize('start_error,refresh_error', [(True, False), (False, True)])
def test_worker_failure_releases_permit_and_pending_marker(start_error, refresh_error):
    namespace, tasks, _ = scheduler(start_error, refresh_error)
    namespace['schedule_dashboard_summary_refresh']('device')
    if tasks:
        with pytest.raises(RuntimeError, match='database offline'):
            tasks[0]()
    assert not namespace['dashboard_summary_refresh_pending']
    assert namespace['telemetry_background_semaphore'].acquire(blocking=False)


def telemetry_scheduler(start_error=False):
    namespace, tasks, refreshed = scheduler(start_error=start_error)
    source = Path(__file__).resolve().parents[1] / 'flask_app' / 'server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'schedule_telemetry_postprocess')
    namespace.update({
        'telemetry_postprocess_lock': threading.Lock(),
        'telemetry_postprocess_pending': {},
        'telemetry_postprocess_running': set(),
        'telemetry_postprocess_last_run_at': {},
        'fcntl': None,
        'postprocess_telemetry_payload': lambda *args: refreshed.append(args[0]['device_id']),
    })
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace, tasks, refreshed


def test_telemetry_schedules_summary_after_releasing_shared_permit():
    namespace, tasks, refreshed = telemetry_scheduler()
    namespace['schedule_telemetry_postprocess']({'device_id': 'device'})
    tasks.pop(0)()
    assert refreshed == ['device']
    assert len(tasks) == 1
    tasks.pop(0)()
    assert refreshed == ['device', 'device']
    assert not namespace['telemetry_postprocess_running']


def test_telemetry_thread_limit_does_not_fail_request_or_stick_running_marker():
    namespace, tasks, _ = telemetry_scheduler(start_error=True)
    namespace['schedule_telemetry_postprocess']({'device_id': 'device'})
    assert not tasks
    assert not namespace['telemetry_postprocess_running']
    assert not namespace['telemetry_postprocess_pending']
