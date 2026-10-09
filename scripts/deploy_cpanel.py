#!/usr/bin/env python3
"""Deploy changed application files over FTPS, restart Passenger and verify."""
import argparse
from datetime import datetime, timezone, timedelta
import ftplib
import getpass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time
import urllib.request
import uuid

from upload_repo_ftps import iter_upload_items, should_include, ensure_remote_directory_tree
from watch_stderr_ftps import connect, disconnect

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_remote(ftp, path):
    buffer = io.BytesIO()
    try:
        ftp.retrbinary(f'RETR {path}', buffer.write)
    except ftplib.error_perm as exc:
        # Only a missing file is acceptable; permission failures must abort.
        if str(exc).startswith('550'):
            # Prove absence using directory listings rather than interpreting
            # a 550 permission error as permission to overwrite a file.
            parent = '.'
            for part in PurePosixPath(path).parts:
                names = ftp.nlst(parent)
                if part not in {PurePosixPath(name.rstrip('/')).name for name in names}:
                    return None
                parent = part if parent == '.' else parent + '/' + part
        raise
    return buffer.getvalue()


def changed_paths():
    result = subprocess.run(['git', 'status', '--porcelain', '-z', '--', '.'],
                            cwd=ROOT, capture_output=True, check=True)
    records = result.stdout.decode('utf-8').split('\0')
    paths = []
    index = 0
    while index < len(records):
        entry = records[index]
        index += 1
        if not entry:
            continue
        path = entry[3:]
        if 'R' in entry[:2] or 'C' in entry[:2]:
            index += 1
        # Git may report paths relative to the workspace instead of ROOT.
        if path.startswith(ROOT.name + '/'):
            path = path[len(ROOT.name) + 1:]
        candidate = ROOT / path
        if candidate.is_dir():
            paths.extend(item.relative_path for item in iter_upload_items(ROOT)
                         if item.relative_path.startswith(path.rstrip('/') + '/'))
        elif candidate.is_file() and should_include(path):
            paths.append(path)
    return sorted(set(paths))


def build_metadata():
    metadata = {}
    for name, arguments in (
        ('git_commit', ['rev-parse', '--short=8', 'HEAD']),
        ('git_branch', ['rev-parse', '--abbrev-ref', 'HEAD']),
        ('build_number', ['rev-list', '--count', 'HEAD']),
    ):
        try:
            result = subprocess.run(['git', *arguments], cwd=ROOT, capture_output=True,
                                    text=True, timeout=5, check=True)
            metadata[name] = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return metadata


def validate_path(path):
    parts = PurePosixPath(path).parts
    if not parts or path.startswith('/') or '..' in parts or '\\' in path:
        raise ValueError(f'Invalid deployment path: {path}')
    if not should_include(path) or path == '.swt-deployment.json':
        raise ValueError(f'Protected deployment path: {path}')
    if not (ROOT / path).resolve().is_relative_to(ROOT.resolve()):
        raise ValueError(f'Path escapes project: {path}')


def replace_remote(ftp, path, data, deployment_id, directories):
    parent = str(PurePosixPath(path).parent)
    if parent != '.':
        ensure_remote_directory_tree(ftp, parent, directories, False)
    staged = path + '.upload-' + deployment_id
    ftp.storbinary(f'STOR {staged}', io.BytesIO(data))
    if digest(read_remote(ftp, staged)) != digest(data):
        raise RuntimeError(f'Upload verification failed: {path}')
    ftp.rename(staged, path)


def verify_site(base_url, deployment_id, attempts=6):
    for attempt in range(attempts):
        checks = []
        try:
            request = urllib.request.Request(base_url.rstrip('/') + '/health?deployment=' + deployment_id,
                                             headers={'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(request, timeout=20) as response:
                health = json.load(response)
            if health.get('status') != 'ok' or health.get('deployment_id') != deployment_id:
                raise RuntimeError('New deployment is not active yet')
            for route in ('/', '/pricing'):
                with urllib.request.urlopen(base_url.rstrip('/') + route + '?deployment=' + deployment_id,
                                             timeout=20) as response:
                    body = response.read()
                    if response.status != 200 or b'<html' not in body.lower():
                        raise RuntimeError(f'Page verification failed: {route}')
                    if route == '/' and b'landing-refresh.css' not in body:
                        raise RuntimeError('Landing page update is missing')
                checks.append(route)
            return {'health': health, 'pages': checks}
        except (OSError, ValueError, RuntimeError) as exc:
            print(f'Verification {attempt + 1}/{attempts}: {exc}', flush=True)
            if attempt + 1 == attempts:
                raise
            time.sleep(5)


def restore_deployment(ftp, directory, base_url):
    directory = directory.resolve()
    if not directory.is_relative_to((ROOT / 'data' / 'deployments').resolve()):
        raise ValueError('Rollback directory must be under data/deployments')
    original = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    directories = set()
    candidates = original['files']
    if 'applied' in original:
        candidates = [item for item in candidates if item['path'] in original['applied']]
    plan = []
    # Refuse to clobber files belonging to a different subsequent deployment.
    for item in candidates:
        path = item['path']
        if path != '.swt-deployment.json':
            validate_path(path)
        previous = directory / 'backup' / path
        old_data = previous.read_bytes() if previous.is_file() else None
        current = read_remote(ftp, path)
        if current == old_data:
            continue
        if current is not None and digest(current) != item['sha256']:
            raise RuntimeError(f'Rollback refused: {path} changed since this deployment')
        plan.append((path, old_data, current))
    for path, old_data, current in reversed(plan):
        if old_data is not None:
            replace_remote(ftp, path, old_data, uuid.uuid4().hex, directories)
        elif current is not None:
            ftp.delete(path)
        print(f'Restored: {path}', flush=True)
    ensure_remote_directory_tree(ftp, 'tmp', directories, False)
    ftp.storbinary('STOR tmp/restart.txt', io.BytesIO(uuid.uuid4().hex.encode()))
    result = {'restored_files': [path for path, _, _ in plan], 'restart_requested': True}
    try:
        with urllib.request.urlopen(base_url.rstrip('/') + '/health', timeout=20) as response:
            result['health'] = json.load(response)
            if result['health'].get('status') != 'ok':
                raise RuntimeError('Restored application health check failed')
        result['verified'] = True
    except (OSError, ValueError, RuntimeError) as exc:
        result['verified'] = False
        result['error'] = str(exc)
    (directory / 'rollback-report.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['verified'] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='ftp.salewell.co.in')
    parser.add_argument('--port', type=int, default=21)
    parser.add_argument('--user', default='logviewer@salewell.co.in')
    parser.add_argument('--insecure-ftps', action='store_true')
    parser.add_argument('--url', default='https://salewell.co.in')
    parser.add_argument('--all', action='store_true', help='deploy all permitted project files')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--watch-seconds', type=int, default=45)
    parser.add_argument('--rollback', type=Path, help='restore a saved data/deployments directory')
    args = parser.parse_args()
    if not 0 <= args.watch_seconds <= 300:
        parser.error('--watch-seconds must be between 0 and 300')
    if args.rollback:
        if args.dry_run:
            print(f'Would restore saved deployment: {args.rollback}')
            return 0
        password = os.environ.get('SWT_FTP_PASSWORD')
        if password is None:
            password = getpass.getpass('FTP password: ')
        args.plain_ftp = False
        ftp = None
        try:
            ftp = connect(args, password)
            return restore_deployment(ftp, args.rollback, args.url)
        except Exception as exc:
            print(f'Rollback failed: {exc}', flush=True)
            return 1
        finally:
            disconnect(ftp)
    sources = {item.relative_path: item.local_path for item in iter_upload_items(ROOT)} if args.all else {}
    paths = sorted(sources) if args.all else changed_paths()
    for required in ('passenger_wsgi.py', 'scripts/collect_host_diagnostics.py'):
        if required not in paths:
            paths.append(required)
    for path in paths:
        validate_path(path)
        if path.endswith('.py'):
            compile(sources.get(path, ROOT / path).read_bytes(), path, 'exec')
    print(json.dumps({'files': paths, 'restart': 'tmp/restart.txt', 'url': args.url}, indent=2), flush=True)
    if args.dry_run:
        return 0
    deployment_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    output = ROOT / 'data' / 'deployments' / deployment_id
    output.mkdir(parents=True)
    password = os.environ.get('SWT_FTP_PASSWORD')
    if password is None:
        password = getpass.getpass('FTP password: ')
    args.plain_ftp = False
    ftp = None
    report = {'id': deployment_id, 'files': [], 'status': 'uploading'}
    directories = set()
    applied = []
    verified = False
    metadata = build_metadata()
    try:
        ftp = connect(args, password)
        # Abort if the account is mapped to a different application.
        if read_remote(ftp, 'passenger_wsgi.py') is None:
            raise RuntimeError('FTP root is not the Flask application directory')
        # Avoid replacing a running deployment when the site's network or
        # hosting services are already unavailable.
        with urllib.request.urlopen(args.url.rstrip('/') + '/health', timeout=20) as response:
            if json.load(response).get('status') != 'ok':
                raise RuntimeError('Existing application health check failed; no files changed')
        ftp.voidcmd('TYPE I')
        log_offset = ftp.size('stderr.log') or 0
        # Back up all targets before changing anything.
        planned = []
        for path in paths + ['.swt-deployment.json']:
            data = json.dumps({'id': deployment_id, **metadata, 'collect_diagnostics': True,
                               'diagnostics_expires': (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()}).encode() if path == '.swt-deployment.json' else sources.get(path, ROOT / path).read_bytes()
            previous = read_remote(ftp, path)
            if previous == data:
                continue
            if previous is not None:
                backup = output / 'backup' / path
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(previous)
            planned.append((path, data))
            report['files'].append({'path': path, 'sha256': digest(data), 'existed': previous is not None})
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        for path, data in planned:
            replace_remote(ftp, path, data, deployment_id, directories)
            applied.append(path)
            report['applied'] = list(applied)
            (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(f'Uploaded and verified: {path}', flush=True)
        ensure_remote_directory_tree(ftp, 'tmp', directories, False)
        ftp.storbinary('STOR tmp/restart.txt', io.BytesIO(deployment_id.encode()))
        report['status'] = 'restarted'
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['verification'] = verify_site(args.url, deployment_id)
        verified = True
        print('New deployment is active. Watching fresh logs...', flush=True)
        deadline = time.monotonic() + args.watch_seconds
        log_path = output / 'stderr-after-deploy.log'
        with log_path.open('ab') as stream:
            while True:
                ftp.voidcmd('TYPE I')
                size = ftp.size('stderr.log') or 0
                if size < log_offset:
                    log_offset = 0
                if size > log_offset:
                    received = [0]
                    def save(chunk):
                        stream.write(chunk)
                        stream.flush()
                        received[0] += len(chunk)
                    ftp.retrbinary('RETR stderr.log', save, rest=log_offset or None)
                    log_offset += received[0]
                if time.monotonic() >= deadline:
                    break
                time.sleep(3)
        for remote, name in (('data/host-diagnostics.json', 'host-diagnostics.json'),
                             ('data/sales_enquiries.queue.status.json', 'booking-status.json')):
            data = read_remote(ftp, remote)
            if data is not None:
                (output / name).write_bytes(data)
        report['status'] = 'verified'
        log = log_path.read_text(encoding='utf-8', errors='replace')
        report['log_summary'] = {'sigterm_lines': log.count('killed by signal: 15'),
                                 'tracebacks': log.count('Traceback (most recent call last)'),
                                 'error_lines': log.count('[ERROR]'),
                                 'mysql_disconnects': log.count('server has gone away')}
        print(json.dumps(report['log_summary'], indent=2))
        if report['log_summary']['tracebacks'] or report['log_summary']['error_lines']:
            report['status'] = 'verified-with-log-errors'
            return 2
        return 0
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        # Try collecting evidence before rollback, while the original FTP
        # connection is still available. Failure must not prevent restoration.
        if ftp is not None and applied:
            try:
                ftp.voidcmd('TYPE I')
                size = ftp.size('stderr.log') or 0
                if size > log_offset:
                    with (output / 'stderr-failed-deploy.log').open('wb') as stream:
                        ftp.retrbinary('RETR stderr.log', stream.write,
                                       rest=max(log_offset, size - 1024 * 1024) or None)
                diagnostics = read_remote(ftp, 'data/host-diagnostics.json')
                if diagnostics is not None:
                    (output / 'host-diagnostics.json').write_bytes(diagnostics)
            except Exception as diagnostic_exc:
                report['diagnostic_error'] = str(diagnostic_exc)
        if ftp is not None and applied and not verified:
            try:
                for path in reversed(applied):
                    backup = output / 'backup' / path
                    if backup.is_file():
                        replace_remote(ftp, path, backup.read_bytes(), deployment_id + '-rollback', directories)
                    else:
                        ftp.delete(path)
                ensure_remote_directory_tree(ftp, 'tmp', directories, False)
                ftp.storbinary('STOR tmp/restart.txt', io.BytesIO((deployment_id + '-rollback').encode()))
                report['rollback'] = 'restored previous files and requested app restart'
            except Exception as rollback_exc:
                report['rollback'] = f'failed: {rollback_exc}'
        print(f'Deployment failed: {exc}. Backups and report: {output}', flush=True)
        return 1
    finally:
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        disconnect(ftp)
        print(f'Deployment report: {output / "report.json"}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
