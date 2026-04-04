from pathlib import Path


exec(Path("flask_app/gunicorn.conf.py").read_text(encoding="utf-8"))
