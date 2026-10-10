"""Observe cPanel worker ancestry and correlate SIGTERM log entries.

Uses only Python's standard library. Does not read command arguments, process
environments, device.env, or database credentials; does not restart the app.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time

from collect_host_diagnostics import process_snapshot


KILLED = re.compile(
    r'\[UID:(\d+)\]\[(\d+)\] Child process with pid: (\d+) '
    r'was killed by signal: (\d+), core dumped: (\w+)'
)
STARTUP = re.compile(r'Loading Passenger entrypoint .*?pid=(\d+) parent_pid=(\d+)')


def parse_log(text):
    events = []
    for line in text.splitlines():
        match = KILLED.search(line)
        if match:
            uid, parent, child, signal, core = match.groups()
            events.append(dict(kind='termination', uid=int(uid), reporting_pid=int(parent),
                               child_pid=int(child), signal=int(signal), core_dumped=core))
        else:
            match = STARTUP.search(line)
            if match:
                events.append(dict(kind='startup', pid=int(match[1]), parent_pid=int(match[2])))
    return events


def snapshot(proc_root=Path('/proc')):
    processes = process_snapshot(proc_root)
    for process in processes:
        directory = proc_root / str(process['pid'])
        try:
            # comm may contain spaces/parentheses. Field 22 is the start tick.
            stat = (directory / 'stat').read_text()
            process['start_ticks'] = int(stat[stat.rfind(')') + 2:].split()[19])
        except (OSError, ValueError, IndexError):
            process['start_ticks'] = None
        try:
            process['executable'] = os.readlink(directory / 'exe')
        except OSError:
            process['executable'] = None
    return processes


def read_log_chunk(path, offset, limit=65536):
    try:
        with path.open('rb') as handle:
            size = os.fstat(handle.fileno()).st_size
            if offset is None or offset > size:
                offset = max(0, size - limit)
            # Bound every read, even when the log suddenly grows rapidly.
            start = max(offset, size - limit)
            handle.seek(start)
            data = handle.read(limit)
            return data.decode('utf-8', errors='replace'), handle.tell(), start > offset
    except OSError:
        return '', offset, False


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
    temporary.replace(path)


def collect(seconds, interval, log_path, output, target_pid=None):
    report = {
        'collector_pid': os.getpid(), 'target_pid': target_pid,
        'limitations': [
            'The termination log identifies the reporting process, not the signal sender.',
            'Already-exited processes cannot be identified without an earlier observation.',
            'Short-lived processes may exit between samples; /proc visibility may be restricted.',
            'The first log sample contains historical entries; PID reuse is possible.',
        ],
        'samples': [], 'log_events': [], 'process_events': [],
    }
    offset = None
    previous = {}
    observed = {}
    deadline = time.monotonic() + seconds
    try:
        while True:
            stamp = datetime.now(timezone.utc).isoformat()
            processes = snapshot()
            current = {(item['pid'], item['start_ticks']): item for item in processes}
            for key in current.keys() - previous.keys():
                report['process_events'].append(dict(kind='first_seen', time=stamp, process=current[key]))
            for key in previous.keys() - current.keys():
                report['process_events'].append(dict(kind='no_longer_visible', time=stamp, process=previous[key]))
            observed.update({item['pid']: item for item in processes})
            historical = offset is None
            chunk, offset, skipped = read_log_chunk(log_path, offset)
            for event in parse_log(chunk):
                event.update(observed_at=stamp, historical=historical)
                if event['kind'] == 'termination':
                    event['last_observed_child'] = observed.get(event['child_pid'])
                    event['last_observed_reporter'] = observed.get(event['reporting_pid'])
                report['log_events'].append(event)
            report['samples'].append(dict(time=stamp, processes=processes, log_bytes_skipped=skipped))
            previous = current
            save_report(output, report)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        report['interrupted'] = True
        save_report(output, report)
    return report


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=45)
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--pid', type=int, help='highlight a child or reporting PID in the summary')
    parser.add_argument('--log', type=Path, default=root / 'stderr.log')
    parser.add_argument('--output', type=Path, default=root / 'data' / 'worker-exits.json')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300 or not 0.1 <= args.interval <= 5:
        parser.error('seconds must be 1–300 and interval must be 0.1–5')
    if not hasattr(os, 'getuid') or not Path('/proc').is_dir():
        parser.error('Run this script on the Linux hosting server with /proc available.')
    if args.output.resolve() == args.log.resolve():
        parser.error('Output must differ from the input log.')
    report = collect(args.seconds, args.interval, args.log, args.output, args.pid)
    events = [event for event in report['log_events'] if event['kind'] == 'termination']
    if args.pid is not None:
        events = [event for event in events if args.pid in (event['child_pid'], event['reporting_pid'])]
    print('Report:', args.output)
    print('Processes visible:', bool(report['samples'] and report['samples'][0]['processes']))
    print('Matching termination entries:', len(events))
    for event in events[-20:]:
        child = event.get('last_observed_child') or {}
        print('Child', event['child_pid'], 'reporter', event['reporting_pid'],
              'signal', event['signal'], 'name', child.get('name', 'not observed'),
              '(historical)' if event['historical'] else '(observed log entry)')
    print('Signal sender is not recorded by these logs; provider tracing is needed to prove it.')


if __name__ == '__main__':
    main()
