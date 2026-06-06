from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


def parse_simple_dotenv(dotenv_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    try:
        with dotenv_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                values[key] = value
    except OSError:
        return {}
    return values


def load_dotenv_values(
    dotenv_paths: Iterable[Path],
    environ: Any = None,
    preserve_existing: bool = True,
) -> None:
    target_env = os.environ if environ is None else environ
    original_keys = set(target_env)
    loaded_keys = set()
    for dotenv_path in dotenv_paths:
        for key, value in parse_simple_dotenv(dotenv_path).items():
            if preserve_existing and key in original_keys and key not in loaded_keys:
                continue
            target_env[key] = value
            loaded_keys.add(key)


def normalize_db_path(raw_path: str | Path, project_root: Path) -> Path:
    db_path = Path(raw_path).expanduser()
    if not db_path.is_absolute():
        db_path = project_root / db_path
    return db_path


def normalize_http_base_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"


def env_flag(name: str, default: bool = False, environ: Any = None) -> bool:
    env_source = os.environ if environ is None else environ
    raw_value = env_source.get(name)
    if raw_value is None:
        return default
    text = str(raw_value).strip()
    if not text:
        return default
    return text.lower() in {"1", "true", "yes", "on"}


def _warn_invalid_env_value(name: str, raw_value: Any, expected_type: str, default: Any) -> None:
    print(
        f"Ignoring invalid {expected_type} environment variable {name}={raw_value!r}; "
        f"using default {default!r}.",
        file=sys.stderr,
    )


def env_int(name: str, default: int, environ: Any = None) -> int:
    env_source = os.environ if environ is None else environ
    raw_value = env_source.get(name)
    if raw_value is None:
        return default

    text = str(raw_value).strip()
    if not text:
        _warn_invalid_env_value(name, raw_value, "integer", default)
        return default

    try:
        return int(text)
    except (TypeError, ValueError):
        _warn_invalid_env_value(name, raw_value, "integer", default)
        return default


def env_float(name: str, default: float, environ: Any = None) -> float:
    env_source = os.environ if environ is None else environ
    raw_value = env_source.get(name)
    if raw_value is None:
        return default

    text = str(raw_value).strip()
    if not text:
        _warn_invalid_env_value(name, raw_value, "float", default)
        return default

    try:
        return float(text)
    except (TypeError, ValueError):
        _warn_invalid_env_value(name, raw_value, "float", default)
        return default


def db_parent_is_writable(db_path: Path) -> bool:
    parent = db_path.parent
    probe = parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        if not os.access(probe, os.W_OK):
            return False
        parent.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False
