"""Restore the configured Flask secret without uploading other environment values."""

import argparse
import getpass
import ftplib
import io
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask_app.runtime_utils import parse_simple_dotenv
from watch_stderr_ftps import connect, disconnect


def patch_secret(data, secret):
    if not secret or any(char in secret for char in '\r\n\x00'):
        raise ValueError('A nonblank, single-line APP_SECRET_KEY is required.')
    text = data.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in text else '\n'
    # Quote the value so dotenv comment parsing preserves the entire secret.
    if '"' in secret or '\\' in secret:
        raise ValueError('Secret contains unsupported quote or escape characters.')
    replacement = 'APP_SECRET_KEY="' + secret + '"'
    pattern = r'(?m)^[ \t]*(?:export[ \t]+)?APP_SECRET_KEY[ \t]*=[^\r\n]*'
    if re.search(pattern, text):
        text = re.sub(pattern, lambda match: replacement, text)
    else:
        text = text.rstrip('\r\n') + newline + replacement + newline
    return text.encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='ftp.salewell.co.in')
    parser.add_argument('--port', type=int, default=21)
    parser.add_argument('--user', default='swt_flask@salewell.co.in')
    parser.add_argument('--remote-root', default='.')
    parser.add_argument('--insecure-ftps', action='store_true')
    args = parser.parse_args()
    args.plain_ftp = False
    root = Path(__file__).resolve().parents[1]
    secret = parse_simple_dotenv(root / 'device.env').get('APP_SECRET_KEY', '').strip()
    if not secret or secret == 'change-me-before-production':
        raise SystemExit('Configure a real APP_SECRET_KEY in the local Flask device.env first.')
    password = os.environ.get('SWT_FTP_PASSWORD') or getpass.getpass('FTP password: ')
    ftp = None
    try:
        ftp = connect(args, password)
        ftp.cwd(args.remote_root)
        # Confirm the destination is the Flask checkout before touching secrets.
        names = {name.rsplit('/', 1)[-1] for name in ftp.nlst()}
        if not {'server.py', 'passenger_wsgi.py', 'flask_app', 'device.env'} <= names:
            raise RuntimeError('Remote root must be the deployed Flask application directory.')
        original = io.BytesIO()
        ftp.retrbinary('RETR device.env', original.write)
        updated = patch_secret(original.getvalue(), secret)
        backup_dir = root / 'data' / 'secret-repairs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup_dir.mkdir(parents=True)
        backup = backup_dir / 'device.env.backup'
        with backup.open('xb') as handle:
            os.chmod(backup, 0o600)
            handle.write(original.getvalue())
        ftp.storbinary('STOR device.env', io.BytesIO(updated))
        verified = io.BytesIO()
        ftp.retrbinary('RETR device.env', verified.write)
        if verified.getvalue() != updated:
            ftp.storbinary('STOR device.env', io.BytesIO(original.getvalue()))
            raise RuntimeError('Verification failed; original configuration restored.')
        try:
            ftp.cwd('tmp')
        except ftplib.error_perm:
            ftp.mkd('tmp')
            ftp.cwd('tmp')
        ftp.storbinary('STOR restart.txt', io.BytesIO(b'secret configuration repaired\n'))
        print('APP_SECRET_KEY updated and verified; Flask restart requested.')
        print('Configuration backup:', backup)
        print('Check https://salewell.co.in/health after the restart.')
    finally:
        disconnect(ftp)


if __name__ == '__main__':
    main()
