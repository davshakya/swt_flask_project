#!/usr/bin/env python3
"""Follow a remote stderr.log over FTP or explicit FTPS."""

from __future__ import annotations

import argparse
import codecs
import ftplib
import getpass
import os
import ssl
import socket
import sys
import time
import zlib
from pathlib import Path


DEFAULT_HOST = "ftp.salewell.co.in"
DEFAULT_USER = "salewell@salewell.co.in"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously print bytes appended to a remote stderr.log."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=21)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--remote-file", default="stderr.log")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true", help="download one snapshot and exit")
    parser.add_argument("--output", type=Path, help="save log contents to this local file")
    parser.add_argument(
        "--tail-bytes",
        type=int,
        default=64 * 1024,
        help="bytes to show initially (default: 65536)",
    )
    parser.add_argument(
        "--from-start", action="store_true", help="read the entire existing log"
    )
    parser.add_argument(
        "--plain-ftp",
        action="store_true",
        help="disable TLS (credentials and log data will be unencrypted)",
    )
    parser.add_argument(
        "--insecure-ftps",
        action="store_true",
        help="use FTPS without verifying its certificate (unsafe; hostname mismatch workaround)",
    )
    parser.add_argument(
        "--password-env",
        default="SWT_FTP_PASSWORD",
        help="environment variable containing the password",
    )
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="allow more than one watcher for the same host/user/log (not recommended)",
    )
    return parser.parse_args()


def acquire_single_instance(args: argparse.Namespace) -> socket.socket | None:
    if args.allow_multiple:
        return None
    identity = f"{args.host}:{args.port}:{args.user}:{args.remote_file}".encode("utf-8")
    lock_port = 39000 + (zlib.crc32(identity) % 2000)
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock_socket.bind(("127.0.0.1", lock_port))
        lock_socket.listen(1)
    except OSError as exc:
        lock_socket.close()
        raise SystemExit(
            "A log watcher for this FTPS target is already running. Stop the existing "
            "watcher instead of opening another one (or pass --allow-multiple explicitly)."
        ) from exc
    return lock_socket


def disconnect(ftp: ftplib.FTP | None) -> None:
    if ftp is None:
        return
    try:
        ftp.quit()
    except ftplib.all_errors + (OSError, EOFError):
        try:
            ftp.close()
        except OSError:
            pass


def connect(args: argparse.Namespace, password: str) -> ftplib.FTP:
    if args.plain_ftp:
        ftp: ftplib.FTP = ftplib.FTP(timeout=30)
    else:
        context = (
            ssl._create_unverified_context()
            if args.insecure_ftps
            else ssl.create_default_context()
        )
        ftp = ftplib.FTP_TLS(context=context, timeout=30)

    ftp.connect(args.host, args.port)
    ftp.login(args.user, password)
    if isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()  # Encrypt file listings and file contents too.
    ftp.voidcmd("TYPE I")
    return ftp


def remote_size(ftp: ftplib.FTP, remote_file: str) -> int:
    ftp.voidcmd("TYPE I")
    size = ftp.size(remote_file)
    if size is None:
        raise RuntimeError(f"FTP server did not report a size for {remote_file!r}")
    return size


def main() -> int:
    args = parse_args()
    if args.interval <= 0 or args.tail_bytes < 0:
        raise SystemExit("--interval must be positive and --tail-bytes cannot be negative")
    if args.plain_ftp and args.insecure_ftps:
        raise SystemExit("--plain-ftp and --insecure-ftps cannot be used together")

    instance_lock = acquire_single_instance(args)
    password = os.environ.get(args.password_env)
    if password is None:
        password = getpass.getpass(f"FTP password for {args.user}: ")

    ftp: ftplib.FTP | None = None
    offset: int | None = None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    reconnect_delay = 5
    output_handle = None
    snapshot_temp = None
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        snapshot_temp = args.output.with_name(f'{args.output.name}.{os.getpid()}.tmp') if args.once else None
        output_handle = (snapshot_temp or args.output).open("w", encoding="utf-8")
    print(
        f"Following {args.remote_file} on {args.host}:{args.port} "
        f"using {'FTP' if args.plain_ftp else 'explicit FTPS'} (Ctrl+C to stop)...",
        file=sys.stderr,
    )
    if args.insecure_ftps:
        print(
            "WARNING: TLS certificate verification is disabled; the server identity "
            "cannot be confirmed.",
            file=sys.stderr,
        )

    try:
        while True:
            try:
                if ftp is None:
                    ftp = connect(args, password)

                size = remote_size(ftp, args.remote_file)
                if offset is None:
                    offset = 0 if args.from_start else max(0, size - args.tail_bytes)
                elif size < offset:
                    print("\n--- remote log was truncated or rotated ---", file=sys.stderr)
                    offset = 0
                    decoder.reset()

                if size > offset:
                    received = 0

                    def display(chunk: bytes) -> None:
                        nonlocal received
                        received += len(chunk)
                        text = decoder.decode(chunk, final=False)
                        if text:
                            print(text, end="", flush=True, file=output_handle or sys.stdout)

                    ftp.retrbinary(
                        f"RETR {args.remote_file}", display, blocksize=64 * 1024, rest=offset
                    )
                    offset += received

                reconnect_delay = 5
                if args.once:
                    if args.output:
                        remainder = decoder.decode(b'', final=True)
                        if remainder:
                            output_handle.write(remainder)
                        output_handle.close()
                        output_handle = None
                        snapshot_temp.replace(args.output)
                        print(f"Log snapshot saved to {args.output}", file=sys.stderr)
                    return 0
                time.sleep(args.interval)
            except ftplib.all_errors + (OSError, EOFError, RuntimeError) as exc:
                action = 'snapshot failed' if args.once else f'retrying in {reconnect_delay} seconds'
                print(
                    f"\n--- log watcher connection error: {type(exc).__name__}: {exc!s}; {action} ---",
                    file=sys.stderr,
                )
                disconnect(ftp)
                ftp = None
                if args.once:
                    return 1
                time.sleep(reconnect_delay)
                reconnect_delay = min(60, reconnect_delay * 2)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        if output_handle is not None:
            output_handle.close()
        if snapshot_temp is not None:
            snapshot_temp.unlink(missing_ok=True)
        disconnect(ftp)
        if instance_lock is not None:
            instance_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
