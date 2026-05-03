import sys

from flask_app import server as flask_server

app = flask_server.app
application = app
flask_server.application = app

sys.modules[__name__] = flask_server
