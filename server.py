import os
import sys

from flask_app import server as flask_server

app = flask_server.app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True)
else:
    # Keep the root compatibility module and the real Flask module in sync.
    sys.modules[__name__] = flask_server
