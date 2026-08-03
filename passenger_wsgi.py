"""cPanel Passenger entry point.

The cPanel configuration intentionally uses this file and the ``application``
callable. Pin imports to this checkout so another cached ``server`` or
``flask_app`` package on the shared host cannot serve an older deployment.
"""
import importlib
import os
from pathlib import Path
import sys


PASSENGER_ENTRYPOINT_REVISION = "2026-08-03.2257-openblas-thread-limit"
PROJECT_ROOT = Path(__file__).resolve().parent
project_path = str(PROJECT_ROOT)

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
sys.stderr.write(f"[SaleWell] Loading Passenger entrypoint {PASSENGER_ENTRYPOINT_REVISION}\n")
sys.stderr.flush()
from server import app as application
