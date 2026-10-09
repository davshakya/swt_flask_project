#!/usr/bin/env python3
"""Collect credential-free process evidence through cPanel Run Script."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time


def process_snapshot(proc_root=Path('/proc')):
    processes = []
    if not hasattr(os, 'getuid') or not proc_root.is_dir():
        return processes
    for directory in proc_root.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = {}
            for line in (directory / 'status').read_text().splitlines():
                key, _, value = line.partition(':')
                fields[key] = value.strip()
            if int(fields['Uid'].split()[0]) != os.getuid():
                continue
            processes.append({
                'pid': int(directory.name), 'parent_pid': int(fields['PPid']),
                'name': fields['Name'], 'state': fields['State'],
                'threads': int(fields.get('Threads', '0')),
                'memory': fields.get('VmRSS', 'unavailable'),
            })
        except (OSError, ValueError, KeyError):
            # Processes may exit while /proc is being read.
            continue
    return sorted(processes, key=lambda item: item['pid'])


def collect(seconds=15):
    output = Path(__file__).resolve().parents[1] / 'data' / 'host-diagnostics.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'collector_pid': os.getpid(), 'samples': []}
    for index in range(seconds):
        report['samples'].append({
            'time': datetime.now(timezone.utc).isoformat(),
            'processes': process_snapshot(),
        })
        # Persist each sample so a terminated collector still leaves evidence.
        temporary = output.with_name(f'{output.name}.{os.getpid()}.tmp')
        temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
        temporary.replace(output)
        if index + 1 < seconds:
            time.sleep(1)
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=15)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 45:
        parser.error('--seconds must be between 1 and 45')
    output, report = collect(args.seconds)
    print(json.dumps({'output_file': str(output), 'samples': len(report['samples']),
                      'process_visibility': bool(report['samples'][0]['processes'])}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
