# Smart Water Tank Flask Backend

This repo contains the Flask backend and related deployment scripts extracted from the original monorepo.

## Included

- `server.py`
- `flask_app/`
- `requirements.txt`
- `requirements-ml.txt`
- `render.yaml`
- `Procfile`
- backend utility scripts

## Setup

Copy:

```powershell
Copy-Item flask_app\\.env.example flask_app\\.env
Copy-Item device.env.example device.env
```

Use `device.env` when you want Flask to read the shared device identity and API key.

## Run Locally

```powershell
python -m venv .venv
. .\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

## Deploy

This repo already includes `render.yaml` and is ready for a Render-style deployment workflow.

## Notes

- Set `SESSION_COOKIE_SECURE=false` for local plain HTTP testing.
- Use persistent storage for SQLite in production.
