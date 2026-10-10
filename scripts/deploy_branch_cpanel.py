#!/usr/bin/env python3
"""Deploy the latest esp32_master_backup commit through cPanel Run Script."""
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BRANCH = 'esp32_master_backup'
URL = 'https://github.com/davshakya/swt_flask_project.git'


def main():
    git = '/usr/local/cpanel/3rdparty/bin/git'
    if not Path(git).is_file():
        git = shutil.which('git')
    if not git:
        raise RuntimeError('Git not found')
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')

    def run(*args):
        p = subprocess.run([git, '-C', str(ROOT), '-c', 'credential.helper=', *args],
                           env=env, capture_output=True, timeout=60)
        if p.returncode:
            raise RuntimeError(p.stderr.decode(errors='replace')[:4000])
        return p.stdout

    if Path(run('rev-parse', '--show-toplevel').decode().strip()).resolve() != ROOT:
        raise RuntimeError('Application folder must be the Git repository root')
    if run('branch', '--show-current').decode().strip() != BRANCH:
        raise RuntimeError('Refusing to switch branches; expected ' + BRANCH)
    if run('diff', '--cached', '--name-only').strip():
        raise RuntimeError('Staged changes exist; preserve and reconcile them first')
    before = run('rev-parse', 'HEAD').decode().strip()
    print('Fetching latest ' + BRANCH, flush=True)
    run('fetch', '--no-tags', URL, 'refs/heads/' + BRANCH)
    target = run('rev-parse', 'FETCH_HEAD').decode().strip()
    run('merge-base', '--is-ancestor', before, target)
    incoming = set(run('ls-tree', '-r', '--name-only', '-z', target).decode().split('\0')) - {''}
    modified = set(run('diff', '--name-only', '-z').decode().split('\0')) - {''}
    untracked = set(run('ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')) - {''}

    def protected(name):
        p = Path(name)
        return (p.parts[0] in {'data', 'tmp', 'mysql-data', 'venv', '.venv'}
                or p.name in {'.env', 'device.env', '.ftpquota', '.swt-deployment.json'}
                or p.suffix in {'.log', '.db', '.sqlite3', '.pem'})

    changes = set(run('diff', '--name-only', '-z', before, target).decode().split('\0')) - {''}
    if any(protected(n) for n in changes | (incoming & untracked)):
        raise RuntimeError('Incoming commit overlaps protected runtime files; deployment stopped')
    if any(protected(n) for n in modified):
        raise RuntimeError('Tracked runtime files have local changes; deployment stopped to preserve them')
    conflicts = modified | (incoming & untracked)
    backup = ROOT / 'data' / 'backups' / datetime.now().strftime('deploy-%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True)
    (backup / 'deployment.json').write_text(json.dumps({'before': before, 'target': target,
                                                     'saved_files': sorted(conflicts)}, indent=2))
    # Complete every backup before changing any working file.
    for name in conflicts:
        source = ROOT / name
        if source.is_symlink():
            raise RuntimeError('Cannot deploy over symlink: ' + name)
        if source.exists():
            dest = backup / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    print('Backups: ' + str(backup), flush=True)
    for name in modified:
        run('restore', '--source=HEAD', '--worktree', '--', name)
    for name in incoming & untracked:
        (ROOT / name).unlink()
    try:
        run('merge', '--ff-only', target)
    except Exception:
        for name in conflicts:
            saved = backup / name
            if saved.is_file():
                dest = ROOT / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, dest)
        raise
    after = run('rev-parse', 'HEAD').decode().strip()
    if after != target:
        raise RuntimeError('Commit verification failed; no restart requested')
    marker = ROOT / 'tmp' / 'restart.txt'
    marker.parent.mkdir(exist_ok=True)
    marker.touch()
    print('Updated to ' + after + '; Flask restart requested.', flush=True)
    for url in ['https://salewell.co.in/health', 'https://salewell.co.in/']:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                print(url + ' HTTP ' + str(response.status), flush=True)
        except Exception as exc:
            print(url + ' verification failed: ' + str(exc), flush=True)
    print('HTTP checks alone do not confirm which commit the running worker loaded.')


if __name__ == '__main__':
    main()
