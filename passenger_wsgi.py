"""cPanel Passenger entry point.

The cPanel configuration intentionally uses this file and the ``application``
callable. Pin imports to this checkout so another cached ``server`` or
``flask_app`` package on the shared host cannot serve an older deployment.
"""
import importlib
import json
import os
from pathlib import Path
import sys
import time


PASSENGER_ENTRYPOINT_REVISION = "2026-10-09-booking-cron-worker"
PROJECT_ROOT = Path(__file__).resolve().parent
project_path = str(PROJECT_ROOT)
os.environ.setdefault('SWT_CPANEL_RUNTIME', 'true')

# Shared cPanel accounts can have a high reported CPU count but a low process
# limit.  Keep optional NumPy/scikit-learn imports from exhausting it during
# Passenger application startup.
for thread_env in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[thread_env] = "1"

# Put this application ahead of globally installed/shared-host packages.
sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != project_path]
sys.path.insert(0, project_path)
os.chdir(project_path)

# Passenger can retain modules between reloads. Remove only conflicting copies
# that were imported from outside this configured application root.
for module_name, module in list(sys.modules.items()):
    if module_name != "server" and module_name != "flask_app" and not module_name.startswith("flask_app."):
        continue
    module_file = getattr(module, "__file__", None)
    if module_file:
        try:
            Path(module_file).resolve().relative_to(PROJECT_ROOT)
            continue
        except ValueError:
            pass
    sys.modules.pop(module_name, None)

importlib.invalidate_caches()
sys.stderr.write(
    f"[SaleWell] Loading Passenger entrypoint {PASSENGER_ENTRYPOINT_REVISION} "
    f"pid={os.getpid()} parent_pid={os.getppid()}\n"
)
sys.stderr.flush()
try:
    deployment = json.loads((PROJECT_ROOT / '.swt-deployment.json').read_text())
except (OSError, ValueError):
    deployment = {}
if not isinstance(deployment, dict):
    deployment = {}
os.environ['SWT_DEPLOYMENT_ID'] = str(deployment.get('id', ''))
for source, target in (('git_commit', 'GIT_COMMIT'), ('git_branch', 'GIT_BRANCH'),
                       ('build_number', 'SWT_BUILD_NUMBER')):
    value = deployment.get(source)
    if isinstance(value, str) and value:
        os.environ.setdefault(target, value)
from server import app as application

# A short, process-local observer uses no subprocesses and records no command
# arguments or environment values. It stops automatically after deployment.
try:
    if deployment.get('collect_diagnostics') and time.time() < float(deployment.get('diagnostics_expires', 0)):
        import threading
        import importlib.util

        def observe_processes():
            try:
                spec = importlib.util.spec_from_file_location(
                    'salewell_host_diagnostics', PROJECT_ROOT / 'scripts' / 'collect_host_diagnostics.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.collect(30)
            except Exception as exc:
                sys.stderr.write(f'[SaleWell] Process diagnostics unavailable: {type(exc).__name__}\n')

        threading.Thread(target=observe_processes, name='deploy-diagnostics', daemon=True).start()
except Exception as exc:
    sys.stderr.write(f'[SaleWell] Process diagnostics unavailable: {type(exc).__name__}\n')
