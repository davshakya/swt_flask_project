import ftplib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('deploy_cpanel', SCRIPTS / 'deploy_cpanel.py')
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


@pytest.mark.parametrize('path', ['device.env', 'data/bookings.json', '../server.py', '/server.py'])
def test_runtime_and_escaping_paths_are_protected(path):
    with pytest.raises(ValueError):
        DEPLOY.validate_path(path)


def test_permission_error_on_existing_file_is_not_treated_as_missing():
    class FTP:
        def retrbinary(self, command, callback):
            raise ftplib.error_perm('550 Permission denied')

        def nlst(self, parent):
            return ['passenger_wsgi.py']

    with pytest.raises(ftplib.error_perm):
        DEPLOY.read_remote(FTP(), 'passenger_wsgi.py')


def test_corrupt_staged_upload_cannot_replace_live_file():
    class FTP:
        renamed = False

        def storbinary(self, command, stream):
            pass

        def retrbinary(self, command, callback):
            callback(b'corrupted')

        def rename(self, source, target):
            self.renamed = True

    ftp = FTP()
    with pytest.raises(RuntimeError, match='verification failed'):
        DEPLOY.replace_remote(ftp, 'server.py', b'expected', 'release', set())
    assert not ftp.renamed


def test_old_health_response_does_not_pass_deployment_verification(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(DEPLOY.urllib.request, 'urlopen',
                        lambda *a, **k: Response(json.dumps({'status': 'ok', 'deployment_id': 'old'}).encode()))
    with pytest.raises(RuntimeError, match='not active'):
        DEPLOY.verify_site('https://example.test', 'new', attempts=1)


def test_rollback_refuses_to_overwrite_later_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(DEPLOY, 'ROOT', tmp_path)
    directory = tmp_path / 'data' / 'deployments' / 'failed'
    directory.mkdir(parents=True)
    (directory / 'report.json').write_text(json.dumps({'files': [
        {'path': 'server.py', 'sha256': DEPLOY.digest(b'failed deployment')}
    ]}))
    monkeypatch.setattr(DEPLOY, 'read_remote', lambda ftp, path: b'later deployment')
    with pytest.raises(RuntimeError, match='changed since'):
        DEPLOY.restore_deployment(object(), directory, 'https://example.test')


def test_rollback_restores_backup_and_requests_only_app_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(DEPLOY, 'ROOT', tmp_path)
    directory = tmp_path / 'data' / 'deployments' / 'failed'
    (directory / 'backup').mkdir(parents=True)
    (directory / 'backup' / 'server.py').write_bytes(b'old code')
    (directory / 'report.json').write_text(json.dumps({'files': [
        {'path': 'server.py', 'sha256': DEPLOY.digest(b'new code')}
    ]}))

    class FTP:
        files = {'server.py': b'new code'}

        def retrbinary(self, command, callback):
            callback(self.files[command[5:]])

        def storbinary(self, command, stream):
            self.files[command[5:]] = stream.read()

        def rename(self, source, target):
            self.files[target] = self.files.pop(source)

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(DEPLOY, 'ensure_remote_directory_tree', lambda *a: None)
    monkeypatch.setattr(DEPLOY.urllib.request, 'urlopen',
                        lambda *a, **k: Response(b'{"status":"ok"}'))
    ftp = FTP()
    assert DEPLOY.restore_deployment(ftp, directory, 'https://example.test') == 0
    assert ftp.files['server.py'] == b'old code'
    assert ftp.files['tmp/restart.txt']
