from __future__ import annotations

import argparse
import fnmatch
import getpass
import os
import ssl
from dataclasses import dataclass
from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path
from typing import Iterable


DEFAULT_SERVER = "ftp.salewell.co.in"
DEFAULT_PORT = 21
DEFAULT_USERNAME = "swt_flask@salewell.co.in"
DEFAULT_REMOTE_ROOT = ""
DEFAULT_PASSWORD_ENV_VAR = "SWT_FTP_PASSWORD"
DEFAULT_LOCAL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INSECURE_FTPS = True

EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".github",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "__pycache__",
    "data",
    "node_modules",
    "sandbox-tmp",
}

EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    "Thumbs.db",
}

EXCLUDED_RELATIVE_PATHS = {
    ".env",
    "conftest.py",
    "pytest.ini",
}

EXCLUDED_RELATIVE_PREFIXES = (
    ".pytest-temp/",
    "tests/",
)

EXCLUDED_FILE_PATTERNS = (
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
)


@dataclass(frozen=True)
class UploadItem:
    local_path: Path
    relative_path: str


def normalize_remote_path(path_text: str) -> str:
    return path_text.replace("\\", "/").strip().strip("/")


def join_remote_path(base_path: str, child_path: str) -> str:
    base_normalized = normalize_remote_path(base_path)
    child_normalized = normalize_remote_path(child_path)
    if not base_normalized:
        return child_normalized
    if not child_normalized:
        return base_normalized
    return f"{base_normalized}/{child_normalized}"


def should_include(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip("/")

    if normalized in EXCLUDED_RELATIVE_PATHS:
        return False

    for prefix in EXCLUDED_RELATIVE_PREFIXES:
        if normalized.startswith(prefix):
            return False

    segments = [segment for segment in normalized.split("/") if segment]
    for segment in segments:
        if segment in EXCLUDED_DIRECTORY_NAMES:
            return False

    file_name = Path(normalized).name
    if file_name in EXCLUDED_FILE_NAMES:
        return False

    for pattern in EXCLUDED_FILE_PATTERNS:
        if fnmatch.fnmatch(file_name, pattern):
            return False

    return True


def iter_upload_items(local_root: Path) -> Iterable[UploadItem]:
    for root, dir_names, file_names in os.walk(local_root, topdown=True):
        dir_names[:] = [
            name
            for name in dir_names
            if name not in EXCLUDED_DIRECTORY_NAMES
            and should_include(str(Path(root, name).relative_to(local_root)))
        ]

        root_path = Path(root)
        for file_name in sorted(file_names):
            full_path = root_path / file_name
            try:
                relative_path = full_path.relative_to(local_root).as_posix()
            except ValueError:
                continue
            if should_include(relative_path):
                yield UploadItem(local_path=full_path, relative_path=relative_path)


def get_password_text(password_arg: str | None, password_env_var: str, username: str, dry_run: bool) -> str:
    if password_arg:
        return password_arg

    env_password = os.environ.get(password_env_var, "").strip()
    if env_password:
        return env_password
    if dry_run:
        return ""
    return getpass.getpass(f"FTP password for {username}: ")


def connect_ftp(server: str, port: int, username: str, password: str, use_plain_ftp: bool):
    if use_plain_ftp:
        ftp = FTP()
        ftp.connect(server, port, timeout=120)
        ftp.login(username, password)
        ftp.set_pasv(True)
        return ftp

    context = ssl.create_default_context()
    ftp = FTP_TLS(context=context)
    ftp.connect(server, port, timeout=120)
    ftp.auth()
    ftp.login(username, password)
    ftp.prot_p()
    ftp.set_pasv(True)
    return ftp


def connect_ftps_insecure(server: str, port: int, username: str, password: str):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    ftp = FTP_TLS(context=context)
    ftp.connect(server, port, timeout=120)
    ftp.auth()
    ftp.login(username, password)
    ftp.prot_p()
    ftp.set_pasv(True)
    return ftp


def ensure_remote_directory_tree(ftp: FTP, remote_path: str, created_directories: set[str], dry_run: bool) -> None:
    normalized = normalize_remote_path(remote_path)
    if not normalized:
        return

    current = ""
    for segment in normalized.split("/"):
        current = join_remote_path(current, segment)
        if current in created_directories:
            continue

        if dry_run:
            print(f"[dry-run] Create directory: {current}")
            created_directories.add(current)
            continue

        original_dir = ftp.pwd()
        try:
            path_so_far = ""
            for part in current.split("/"):
                path_so_far = join_remote_path(path_so_far, part)
                try:
                    ftp.cwd(part)
                except error_perm:
                    ftp.mkd(part)
                    ftp.cwd(part)
                created_directories.add(path_so_far)
        finally:
            ftp.cwd(original_dir)


def upload_file(
    ftp: FTP,
    local_path: Path,
    remote_path: str,
    created_directories: set[str],
    dry_run: bool,
) -> None:
    remote_directory = normalize_remote_path(str(Path(remote_path).parent))
    remote_name = Path(remote_path).name

    if remote_directory and remote_directory != ".":
        ensure_remote_directory_tree(ftp, remote_directory, created_directories, dry_run)

    if dry_run:
        print(f"[dry-run] Upload {local_path} -> {normalize_remote_path(remote_path)}")
        return

    original_dir = ftp.pwd()
    try:
        if remote_directory:
            for segment in remote_directory.split("/"):
                ftp.cwd(segment)

        with local_path.open("rb") as handle:
            ftp.storbinary(f"STOR {remote_name}", handle)
        print(f"Uploaded: {local_path} -> {normalize_remote_path(remote_path)}")
    finally:
        ftp.cwd(original_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload this repo over FTP/explicit FTPS.")
    parser.add_argument(
        "password",
        nargs="?",
        help="FTP password. If omitted, the script uses the environment variable or prompts interactively.",
    )
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--local-root", default=str(DEFAULT_LOCAL_ROOT))
    parser.add_argument("--password-env-var", default=DEFAULT_PASSWORD_ENV_VAR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--use-plain-ftp", action="store_true")
    parser.add_argument(
        "--insecure-ftps",
        action="store_true",
        help="Disable FTPS certificate validation. Use only when the host certificate name does not match.",
    )
    parser.add_argument(
        "--secure-ftps",
        action="store_true",
        help="Force normal FTPS certificate validation even if the default compatibility mode is enabled.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    parsed = parser.parse_args()

    local_root = Path(parsed.local_root).resolve()
    remote_root = normalize_remote_path(parsed.remote_root)
    upload_items = sorted(iter_upload_items(local_root), key=lambda item: item.relative_path)
    use_insecure_ftps = (
        not parsed.use_plain_ftp
        and (parsed.insecure_ftps or (DEFAULT_INSECURE_FTPS and not parsed.secure_ftps))
    )
    remote_root_display = remote_root if remote_root else "(FTP login directory)"

    print(f"Local root : {local_root}")
    print(f"Server     : {parsed.server}:{parsed.port}")
    print(f"Username   : {parsed.username}")
    print(f"Remote root: {remote_root_display}")
    if parsed.use_plain_ftp:
        protocol_label = "FTP"
    elif use_insecure_ftps:
        protocol_label = "Explicit FTPS (certificate validation disabled)"
    else:
        protocol_label = "Explicit FTPS"
    print(f"Protocol   : {protocol_label}")
    if parsed.dry_run:
        print("Mode       : Dry run")
    print(f"Files selected: {len(upload_items)}")

    created_directories: set[str] = set()

    if parsed.dry_run:
        ftp = None
    else:
        password_text = get_password_text(parsed.password, parsed.password_env_var, parsed.username, parsed.dry_run)
        if not password_text:
            raise SystemExit(
                f"FTP password is required. Pass it as the first argument, set the {parsed.password_env_var} environment variable, or enter it when prompted."
            )
        if parsed.use_plain_ftp:
            ftp = connect_ftp(
                server=parsed.server,
                port=parsed.port,
                username=parsed.username,
                password=password_text,
                use_plain_ftp=True,
            )
        elif use_insecure_ftps:
            ftp = connect_ftps_insecure(
                server=parsed.server,
                port=parsed.port,
                username=parsed.username,
                password=password_text,
            )
        else:
            ftp = connect_ftp(
                server=parsed.server,
                port=parsed.port,
                username=parsed.username,
                password=password_text,
                use_plain_ftp=False,
            )

    try:
        if ftp is not None:
            ensure_remote_directory_tree(ftp, remote_root, created_directories, parsed.dry_run)
        else:
            ensure_remote_directory_tree(DummyFTP(), remote_root, created_directories, parsed.dry_run)

        for item in upload_items:
            remote_path = join_remote_path(remote_root, item.relative_path)
            upload_file(
                ftp=ftp if ftp is not None else DummyFTP(),
                local_path=item.local_path,
                remote_path=remote_path,
                created_directories=created_directories,
                dry_run=parsed.dry_run,
            )
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    print("Upload complete.")
    return 0


class DummyFTP:
    def pwd(self) -> str:
        return "/"

    def cwd(self, _path: str) -> None:
        return None

    def mkd(self, _path: str) -> None:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
