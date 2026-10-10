#!/usr/bin/env python3
"""Back up and restore only the two application files from the current HEAD."""
from datetime import datetime
from pathlib import Path
import shutil
import subprocess


def main():
    root = Path(__file__).resolve().parents[1]
    files = ['flask_app/server.py', 'passenger_wsgi.py']
    git = '/usr/local/cpanel/3rdparty/bin/git'
    if not Path(git).is_file():
        git = shutil.which('git')
    if not git:
        raise SystemExit('Git was not found; no files were changed.')

    # Confirm this directory is the repository root before restoring anything.
    result = subprocess.run([git, '-C', str(root), 'rev-parse', '--show-toplevel'],
                            check=True, capture_output=True, text=True, timeout=15)
    if Path(result.stdout.strip()).resolve() != root:
        raise SystemExit('The application directory is not the repository root; no files were changed.')

    backup = root / 'data' / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for name in files:
        destination = backup / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    print(f'Backups saved: {backup}', flush=True)

    subprocess.run([git, '-C', str(root), 'restore', '--source=HEAD',
                    '--worktree', '--', *files], check=True, timeout=15)
    print('Restored flask_app/server.py and passenger_wsgi.py from current HEAD.')
    print('No app restart was requested.')


if __name__ == '__main__':
    main()
