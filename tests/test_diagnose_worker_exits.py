import importlib.util
from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('diagnose_worker_exits', SCRIPTS / 'diagnose_worker_exits.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


def test_log_extracts_parent_and_signal_without_saving_unrelated_secrets():
    events = diagnostics.parse_log(
        '[SaleWell] Loading Passenger entrypoint revision pid=1053790 parent_pid=891989\n'
        'PASSWORD=never-save-this\n'
        '[UID:5272][1053790] Child process with pid: 1053988 was killed by signal: 15, core dumped: no\n'
    )
    assert events[0] == dict(kind='startup', pid=1053790, parent_pid=891989)
    assert events[1]['reporting_pid'] == 1053790
    assert events[1]['child_pid'] == 1053988
    assert events[1]['signal'] == 15
    assert 'never-save-this' not in str(events)


def test_log_tail_handles_missing_file_and_truncation(tmp_path):
    log = tmp_path / 'stderr.log'
    assert diagnostics.read_log_chunk(log, None) == ('', None, False)
    log.write_bytes(b'old\n')
    assert diagnostics.read_log_chunk(log, None)[:2] == ('old\n', 4)
    log.write_bytes(b'new')
    assert diagnostics.read_log_chunk(log, 4)[:2] == ('new', 3)
    log.write_bytes(b'0123456789')
    assert diagnostics.read_log_chunk(log, 0, limit=4) == ('6789', 10, True)


def test_process_exit_keeps_identity_and_report_does_not_claim_signal_sender(tmp_path, monkeypatch):
    child = dict(pid=1053988, parent_pid=1053790, start_ticks=100, name='python')
    samples = iter([[child], []])
    monkeypatch.setattr(diagnostics, 'snapshot', lambda: next(samples))
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(diagnostics.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(diagnostics.time, 'sleep', lambda _: None)
    log = tmp_path / 'stderr.log'
    log.write_text('[UID:5272][1053790] Child process with pid: 1053988 was killed by signal: 15, core dumped: no\n')
    output = tmp_path / 'report.json'
    report = diagnostics.collect(1, .5, log, output)
    assert report['log_events'][0]['last_observed_child'] == child
    assert report['process_events'][-1]['kind'] == 'no_longer_visible'
    assert output.exists()
    assert any('not the signal sender' in item for item in report['limitations'])
