import os
import sys

PROJECT_ROOT = os.path.dirname(__file__)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask_app import server as flask_server

app = flask_server.app
application = app
flask_server.application = app
