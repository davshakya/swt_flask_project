#!/usr/bin/env python3
"""Download recent server logs and diagnostic reports for local analysis."""
import argparse
import ftplib
import getpass
import json
import os
import time
from pathlib import Path

from watch_stderr_ftps import connect, disconnect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='ftp.salewell.co.in')
    parser.add_argument('--port', type=int, default=21)
    parser.add_argument('--user', default='logviewer@salewell.co.in')
    parser.add_argument('--insecure-ftps', action='store_true')
    parser.add_argument('--tail-bytes', type=int, default=1024 * 1024)
    args = parser.parse_args()
    if args.tail_bytes <= 0:
        parser.error('--tail-bytes must be positive')
    args.plain_ftp = False
    password = os.environ.get('SWT_FTP_PASSWORD')
    if password is None:
        password = getpass.getpass('FTP password: ')
    output = Path(__file__).resolve().parents[1] / 'data' / 'log-review'
    output.mkdir(parents=True, exist_ok=True)
    ftp = None
    results = []
    try:
        for remote, name in (
            ('stderr.log', 'stderr-latest.log'),
            ('data/host-diagnostics.json', 'host-diagnostics.json'),
            ('data/sales_enquiries.queue.status.json', 'booking-status.json'),
        ):
            for attempt in range(2):
                try:
                    if ftp is None:
                        ftp = connect(args, password)
                    ftp.voidcmd('TYPE I')
                    size = ftp.size(remote)
                    offset = max(0, size - args.tail_bytes) if remote == 'stderr.log' else 0
                    target = output / name
                    temporary = target.with_suffix(target.suffix + '.tmp')
                    with temporary.open('wb') as stream:
                        ftp.retrbinary(f'RETR {remote}', stream.write, rest=offset or None)
                    temporary.replace(target)
                    results.append({'remote': remote, 'status': 'downloaded',
                                    'local': str(target), 'bytes': target.stat().st_size})
                    break
                except (ftplib.Error, OSError, EOFError) as exc:
                    disconnect(ftp)
                    ftp = None
                    if isinstance(exc, ftplib.error_perm) or attempt == 1:
                        results.append({'remote': remote, 'status': 'unavailable', 'reason': str(exc)})
                        break
                    time.sleep(2)
    finally:
        disconnect(ftp)
    print(json.dumps(results, indent=2))
    return 0 if results and results[0]['status'] == 'downloaded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
