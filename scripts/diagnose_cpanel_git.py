#!/usr/bin/env python3
"""Read-only Git/network diagnostics; run through cPanel's Python selector."""
import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def redact(text):
    text = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[redacted]@', text)
    text = re.sub(r'(?i)((?:password|secret|token|api_key|device_keys)\s*[=:]\s*)[^\s,]+', r'\1[redacted]', text)
    return text


def git_check(binary, args):
    started = time.monotonic()
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_ASKPASS='/bin/false')
    try:
        result = subprocess.run([binary, '-C', str(ROOT), *args], env=env,
                                capture_output=True, text=True, errors='replace', timeout=10)
        return {'elapsed_seconds': round(time.monotonic() - started, 2),
                'exit_code': result.returncode, 'stdout': redact(result.stdout[:6000]),
                'stderr': redact(result.stderr[:6000])}
    except subprocess.TimeoutExpired as exc:
        partial = exc.stderr or b''
        if isinstance(partial, bytes):
            partial = partial.decode('utf-8', errors='replace')
        return {'elapsed_seconds': round(time.monotonic() - started, 2),
                'stderr': redact(partial[:6000]),
                'error': 'Git command exceeded 10 seconds; no interactive login permitted.'}
    except Exception as exc:
        return {'error': redact(str(exc))}


def github_https():
    try:
        request = urllib.request.Request('https://github.com/davshakya/swt_flask_project',
                                         headers={'User-Agent': 'SWT-host-diagnostics'})
        with urllib.request.urlopen(request, timeout=8) as response:
            return {'status': response.status}
    except Exception as exc:
        return {'error': redact(str(exc))}


def main():
    report = {'time_utc': datetime.now(timezone.utc).isoformat(), 'app_root': str(ROOT),
              'python': sys.version.split()[0], 'diagnostic_version': 2, 'checks': {}}
    binary = '/usr/local/cpanel/3rdparty/bin/git'
    if not Path(binary).is_file():
        binary = shutil.which('git')
    report['git_binary'] = binary
    checks = report['checks']
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {'github_https': pool.submit(github_https)}
        if binary:
            for name, args in [('branch', ['branch', '--show-current']),
                               ('head', ['rev-parse', '--short', 'HEAD']),
                               ('working_tree', ['status', '--short', '--untracked-files=normal']),
                               ('remote_access', ['-c', 'credential.helper=', 'ls-remote',
                                                  'https://github.com/davshakya/swt_flask_project.git',
                                                  'refs/heads/esp32_master_backup'])]:
                jobs[name] = pool.submit(git_check, binary, args)
        for name, job in jobs.items():
            checks[name] = job.result()
    # Run comparisons sequentially so network checks do not compete with each other.
    if binary:
        checks['configured_remote_access'] = git_check(binary, ['ls-remote', 'origin',
                                                               'refs/heads/esp32_master_backup'])
        checks['origin_without_helpers'] = git_check(binary, ['-c', 'credential.helper=',
                                                              'ls-remote', 'origin',
                                                              'refs/heads/esp32_master_backup'])
    report['deployment_file_exists'] = (ROOT / '.cpanel.yml').is_file()
    log = ROOT / 'stderr.log'
    if log.is_file():
        with log.open('rb') as stream:
            stream.seek(0, 2)
            report['stderr_bytes'] = stream.tell()
            stream.seek(max(0, stream.tell() - 65536))
            lines = stream.read().decode('utf-8', errors='replace').splitlines()
        report['recent_log_evidence'] = [redact(line) for line in lines
                                         if any(word in line for word in
                                                ('Traceback', '[ERROR]', '[WARNING]', 'signal: 15'))][-15:]
    findings = report['findings'] = []
    remote = checks.get('remote_access', {})
    if remote.get('exit_code') == 0:
        findings.append('Direct GitHub Git access succeeds from this server. If cPanel still fails, inspect its own timeout/error log.')
    else:
        findings.append('Direct GitHub Git access failed or Git is unavailable; inspect remote_access.stderr/error.')
    configured = checks.get('configured_remote_access', {})
    no_helpers = checks.get('origin_without_helpers', {})
    if configured.get('exit_code') != 0 and no_helpers.get('exit_code') == 0:
        findings.append('Origin succeeds with credential helpers disabled but fails with configured helpers. Investigate helper configuration; intermittent networking is also possible.')
    if configured.get('exit_code') == 0 and configured.get('elapsed_seconds', 0) > 5:
        findings.append('Configured Git access succeeds but takes over 5 seconds; this could exceed cPanel UI timeout limits.')
    if checks.get('working_tree', {}).get('stdout', '').strip():
        findings.append('Working tree contains local changes. Preserve these before checkout/pull; do not reset or clean blindly.')
    if not report['deployment_file_exists']:
        findings.append('.cpanel.yml is missing from this checkout; Deploy HEAD remains unavailable until it is pulled.')
    output = ROOT / 'data' / 'git-diagnostics.json'
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['report_file'] = str(output)
    except OSError as exc:
        report['report_save_error'] = str(exc)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
