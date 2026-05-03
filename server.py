from flask_app import server as flask_server

app = flask_server.app
application = app
flask_server.application = app


def __getattr__(name):
    return getattr(flask_server, name)


def __dir__():
    return sorted(set(globals()) | set(dir(flask_server)))
