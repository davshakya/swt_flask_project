from __future__ import annotations

import argparse
import fnmatch
import getpass
import os
import re
import socket
import ssl
from dataclasses import dataclass
from ftplib import FTP, FTP_TLS, error_perm, parse227, parse229
from pathlib import Path
from typing import Iterable


DEFAULT_SERVER = "ftp.salewell.co.in"
DEFAULT_PORT = 21
DEFAULT_USERNAME = "swt_flask@salewell.co.in"
DEFAULT_REMOTE_ROOT = ""
DEFAULT_PASSWORD_ENV_VAR = "SWT_FTP_PASSWORD"
DEFAULT_LOCAL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INSECURE_FTPS = True
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_PASSIVE_COMMAND = "epsv"
DEFAULT_PASSWORD_FILE_CANDIDATES = (
    ".env",
    "device.env",
    "flask_app/.env",
)
PROTECTED_RUNTIME_HINT = (
    "Runtime config and data are protected by default: .env files, device.env, "
    "database files, data directories, MySQL data, logs, and local artifacts are skipped."
)

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
    "artifacts",
    "data",
    "mysql-data",
    "node_modules",
    "sandbox-tmp",
    "tmp",
}

EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    ".env",
    "device.env",
    "Thumbs.db",
    "run_local.py"
}

EXCLUDED_RELATIVE_PATHS = {
    ".env",
    "conftest.py",
    "device.env",
    "flask_app/.env",
    "pytest.ini",
}

EXCLUDED_RELATIVE_PREFIXES = (
    ".pytest-temp/",
    "tests/",
)

EXCLUDED_FILE_PATTERNS = (
    "*.env",
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "*.pem",
)


@dataclass(frozen=True)
class UploadItem:
    local_path: Path
    relative_path: str


class ForceEpsvMixin:
    def makepasv(self):  # type: ignore[no-untyped-def]
        return parse229(self.sendcmd("EPSV"), self.sock.getpeername())


class EpsvFTP(ForceEpsvMixin, FTP):
    pass


class EpsvFTP_TLS(ForceEpsvMixin, FTP_TLS):
    pass


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


def iter_protected_runtime_candidates(local_root: Path) -> Iterable[str]:
    candidates = (
        ".env",
        "device.env",
        "flask_app/.env",
        "data",
        "mysql-data",
        "artifacts",
    )
    for relative_path in candidates:
        if (local_root / relative_path).exists():
            yield relative_path


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


def get_password_text(
    password_env_var: str,
    username: str,
    local_root: Path,
    dry_run: bool,
) -> str:
    env_password = os.environ.get(password_env_var, "").strip()
    if env_password:
        return env_password

    file_password = load_password_env_from_runtime_files(local_root, password_env_var)
    if file_password:
        return file_password

    if dry_run:
        return ""
    return getpass.getpass(f"FTP password for {username}: ")


def parse_env_file_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        name, value = stripped.split("=", 1)
        if name.strip() != key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()

    return ""


def load_password_env_from_runtime_files(local_root: Path, password_env_var: str) -> str:
    for relative_path in DEFAULT_PASSWORD_FILE_CANDIDATES:
        path = local_root / relative_path
        if not path.is_file():
            continue

        password = parse_env_file_value(path, password_env_var)
        if password:
            print(f"Password   : loaded from protected local config {relative_path}")
            return password

    return ""


def connect_ftp(
    server: str,
    port: int,
    username: str,
    password: str,
    use_plain_ftp: bool,
    timeout_seconds: float,
    passive_mode: bool,
    force_epsv: bool,
):
    if use_plain_ftp:
        ftp_class = EpsvFTP if force_epsv else FTP
        ftp = ftp_class()
        ftp.connect(server, port, timeout=timeout_seconds)
        ftp.login(username, password)
        ftp.set_pasv(passive_mode)
        return ftp

    context = ssl.create_default_context()
    ftp_class = EpsvFTP_TLS if force_epsv else FTP_TLS
    ftp = ftp_class(context=context)
    ftp.connect(server, port, timeout=timeout_seconds)
    ftp.auth()
    ftp.login(username, password)
    ftp.prot_p()
    ftp.set_pasv(passive_mode)
    return ftp


def connect_ftps_insecure(
    server: str,
    port: int,
    username: str,
    password: str,
    timeout_seconds: float,
    passive_mode: bool,
    force_epsv: bool,
):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    ftp_class = EpsvFTP_TLS if force_epsv else FTP_TLS
    ftp = ftp_class(context=context)
    ftp.connect(server, port, timeout=timeout_seconds)
    ftp.auth()
    ftp.login(username, password)
    ftp.prot_p()
    ftp.set_pasv(passive_mode)
    return ftp


def probe_tcp_connect(host: str, port: int, timeout_seconds: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True, "connected"
    except OSError as exc:
        return False, str(exc)


def parse_epsv_port(response: str) -> int:
    match = re.search(r"\(\|\|\|(\d+)\|\)", response)
    if not match:
        raise ValueError(f"Could not parse EPSV response: {response}")
    return int(match.group(1))


def diagnose_data_connection(
    ftp: FTP,
    server: str,
    control_port: int,
    timeout_seconds: float,
) -> int:
    print("Data probe : checking server-advertised passive data ports")
    failures = 0

    for command in ("EPSV", "PASV"):
        try:
            response = ftp.sendcmd(command)
            if command == "EPSV":
                host = ftp.sock.getpeername()[0]
                data_port = parse_epsv_port(response)
            else:
                host, data_port = parse227(response)
        except Exception as exc:
            failures += 1
            print(f"{command:<10}: failed to request passive endpoint: {exc}")
            continue

        ok, message = probe_tcp_connect(host, data_port, timeout_seconds)
        status = "ok" if ok else "failed"
        print(f"{command:<10}: {response}")
        print(f"{command:<10}: testing {host}:{data_port} -> {status}: {message}")
        if not ok:
            failures += 1

    control_ok, control_message = probe_tcp_connect(server, control_port, timeout_seconds)
    control_status = "ok" if control_ok else "failed"
    print(f"Control   : testing {server}:{control_port} -> {control_status}: {control_message}")

    if failures:
        print(
            "Diagnosis : FTP control login works, but one or more passive data ports are unreachable. "
            "The hosting provider must open/fix the FTP passive port range for this account/server, "
            "or uploads must run from a network where active FTP can accept inbound server connections."
        )
        return 1

    print("Diagnosis : passive data ports are reachable from this machine.")
    return 0


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


def build_transfer_error_message(
    item: UploadItem,
    passive_mode: bool,
    passive_command: str,
    exc: BaseException,
) -> str:
    transfer_mode = "passive" if passive_mode else "active"
    error_text = str(exc)

    private_active_rejected = (
        not passive_mode
        and "open a connection" in error_text
        and any(private_prefix in error_text for private_prefix in ("192.168.", "10.", "172.16."))
    )

    if private_active_rejected:
        hint = (
            "Active mode was rejected because your computer advertised a private LAN address. "
            "Use passive mode and ask the hosting provider to open/fix the FTPS passive data port range, "
            "or run the uploader from a network/server with a directly reachable public IP."
        )
    elif passive_mode:
        fallback_hint = (
            " You can also try --passive-command pasv if this failure happened with EPSV."
            if passive_command == "epsv"
            else " You can also try --passive-command epsv if this failure happened with PASV."
        )
        hint = (
            "Try rerunning with --active-mode. If active mode is rejected because of a private LAN address, "
            "the hosting provider needs to open/fix the FTPS passive data port range."
            f"{fallback_hint}"
        )
    else:
        hint = (
            "Try rerunning without --active-mode. This returns to passive FTP data connections. "
            "If passive mode times out, ask the hosting provider to confirm that FTPS passive data ports are open."
        )

    return (
        "Upload failed while opening or using the FTP data connection.\n"
        f"File       : {item.relative_path}\n"
        f"Mode       : {transfer_mode}\n"
        f"Error      : {exc}\n\n"
        f"{hint}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload this repo over FTP/explicit FTPS.")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--local-root", default=str(DEFAULT_LOCAL_ROOT))
    parser.add_argument(
        "--password-env-var",
        default=DEFAULT_PASSWORD_ENV_VAR,
        help=(
            "Environment/config key used for the FTP password. The script checks the process environment, "
            "then protected local config files such as device.env."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--use-plain-ftp", action="store_true")
    parser.add_argument(
        "--active-mode",
        action="store_true",
        help="Use active data connections instead of passive mode. Try this if STOR uploads time out.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Socket timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS:g}.",
    )
    parser.add_argument(
        "--passive-command",
        choices=("epsv", "pasv"),
        default=DEFAULT_PASSIVE_COMMAND,
        help=f"Passive data command to use. Default: {DEFAULT_PASSIVE_COMMAND.upper()}.",
    )
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
    parser.add_argument(
        "--diagnose-data-connection",
        action="store_true",
        help="Log in and test the server's advertised passive data ports without uploading files.",
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
    passive_mode = not parsed.active_mode
    force_epsv = passive_mode and parsed.passive_command == "epsv"

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
    if passive_mode:
        print(f"Transfer   : passive {parsed.passive_command.upper()} data connections")
    else:
        print("Transfer   : active data connections")
    print(f"Timeout    : {parsed.timeout:g} seconds")
    print(PROTECTED_RUNTIME_HINT)
    if parsed.dry_run:
        print("Mode       : Dry run")
    print(f"Files selected: {len(upload_items)}")
    protected_candidates = list(iter_protected_runtime_candidates(local_root))
    if protected_candidates:
        print("Protected runtime paths present locally and skipped:")
        for relative_path in protected_candidates:
            print(f"  - {relative_path}")

    created_directories: set[str] = set()

    if parsed.dry_run:
        ftp = None
    else:
        password_text = get_password_text(
            parsed.password_env_var,
            parsed.username,
            local_root,
            parsed.dry_run,
        )
        if not password_text:
            raise SystemExit(
                f"FTP password is required. Set {parsed.password_env_var} in your environment or protected local config, or enter it when prompted."
            )
        if parsed.use_plain_ftp:
            ftp = connect_ftp(
                server=parsed.server,
                port=parsed.port,
                username=parsed.username,
                password=password_text,
                use_plain_ftp=True,
                timeout_seconds=parsed.timeout,
                passive_mode=passive_mode,
                force_epsv=force_epsv,
            )
        elif use_insecure_ftps:
            ftp = connect_ftps_insecure(
                server=parsed.server,
                port=parsed.port,
                username=parsed.username,
                password=password_text,
                timeout_seconds=parsed.timeout,
                passive_mode=passive_mode,
                force_epsv=force_epsv,
            )
        else:
            ftp = connect_ftp(
                server=parsed.server,
                port=parsed.port,
                username=parsed.username,
                password=password_text,
                use_plain_ftp=False,
                timeout_seconds=parsed.timeout,
                passive_mode=passive_mode,
                force_epsv=force_epsv,
            )

    if parsed.diagnose_data_connection:
        if ftp is None:
            raise SystemExit("Data connection diagnosis requires a real FTP connection, not --dry-run.")
        try:
            return diagnose_data_connection(ftp, parsed.server, parsed.port, parsed.timeout)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    try:
        if ftp is not None:
            ensure_remote_directory_tree(ftp, remote_root, created_directories, parsed.dry_run)
        else:
            ensure_remote_directory_tree(DummyFTP(), remote_root, created_directories, parsed.dry_run)

        for item in upload_items:
            remote_path = join_remote_path(remote_root, item.relative_path)
            try:
                upload_file(
                    ftp=ftp if ftp is not None else DummyFTP(),
                    local_path=item.local_path,
                    remote_path=remote_path,
                    created_directories=created_directories,
                    dry_run=parsed.dry_run,
                )
            except (TimeoutError, OSError, error_perm) as exc:
                if parsed.dry_run:
                    raise
                raise SystemExit(build_transfer_error_message(item, passive_mode, parsed.passive_command, exc)) from exc
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
