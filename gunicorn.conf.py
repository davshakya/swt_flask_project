from pathlib import Path


exec((Path(__file__).resolve().parent / "flask_app" / "gunicorn.conf.py").read_text(encoding="utf-8"))
