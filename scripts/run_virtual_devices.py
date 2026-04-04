#!/usr/bin/env python
"""Run virtual devices from existing env files without generating new ones."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import virtual_device


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run virtual devices from existing .env files.",
    )
    parser.add_argument(
        "--env-dir",
        default="",
        help=(
            "Directory containing virtual device .env files. "
            "Defaults to tests/virtual_devices/generated, then falls back to tests/virtual_devices."
        ),
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="Run one or more explicit virtual device .env files. May be passed multiple times.",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional backend base URL override applied to every loaded env file.",
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        default=None,
        help="Optional runtime override applied to every loaded env file. Use 0 to run forever.",
    )
    parser.add_argument(
        "--log-level",
        default=virtual_device.env_choice(
            "SWT_VIRTUAL_DEVICE_LOG_LEVEL",
            allowed={"debug", "info", "warning", "error"},
            default="info",
        ).upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Global log level for the fleet runner.",
    )
    return parser


def resolve_env_paths(args: argparse.Namespace) -> list[Path]:
    if args.env_file:
        env_paths = [virtual_device.resolve_runtime_path(item) for item in args.env_file]
        missing = [str(path) for path in env_paths if not path.exists() or not path.is_file()]
        if missing:
            raise SystemExit(f"virtual device env file not found: {', '.join(missing)}")
        return env_paths

    candidate_dirs: list[Path] = []
    if str(args.env_dir or "").strip():
        candidate_dirs.append(virtual_device.resolve_runtime_path(args.env_dir))
    else:
        candidate_dirs.extend(
            [
                virtual_device.DEFAULT_GENERATED_VIRTUAL_DEVICE_ENV_DIR,
                virtual_device.DEFAULT_VIRTUAL_DEVICE_ENV_DIR,
            ]
        )

    for env_dir in candidate_dirs:
        discovered = virtual_device.discover_virtual_device_env_files(env_dir)
        if discovered:
            return discovered

    if virtual_device.LEGACY_VIRTUAL_DEVICE_ENV_PATH.exists():
        return [virtual_device.LEGACY_VIRTUAL_DEVICE_ENV_PATH]

    raise SystemExit(
        "No virtual device env files found. "
        "Generate them first with `python scripts/generate_virtual_device_envs.py --device_count N` "
        "or pass --env-file."
    )


def build_override_env(args: argparse.Namespace) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if str(args.base_url or "").strip():
        overrides["SWT_VIRTUAL_DEVICE_BASE_URL"] = virtual_device.normalize_base_url(args.base_url)
    if args.run_seconds is not None:
        overrides["SWT_VIRTUAL_DEVICE_RUN_SECONDS"] = str(max(0.0, float(args.run_seconds)))
    return overrides


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    env_paths = resolve_env_paths(args)
    virtual_device.log_local_registry_sync(
        virtual_device.sync_virtual_device_envs_to_local_registry(
            env_paths,
            base_environ=os.environ,
        )
    )
    override_env = build_override_env(args)
    configs = virtual_device.build_configs_from_env_paths(
        env_paths,
        override_env=override_env,
        base_environ=os.environ,
    )

    if len(configs) == 1:
        logging.getLogger("virtual-device").info("Using virtual device env file %s", env_paths[0])
        try:
            virtual_device.start_virtual_device(configs[0])
        except KeyboardInterrupt:
            logging.getLogger("virtual-device").info("Virtual device stopped by user.")
            return 0
        return 0

    common_parent = env_paths[0].parent if all(path.parent == env_paths[0].parent for path in env_paths) else None
    if common_parent is not None:
        logging.getLogger("virtual-device").info(
            "Using %s virtual device env files from %s",
            len(env_paths),
            common_parent,
        )
    else:
        logging.getLogger("virtual-device").info("Using %s explicit virtual device env files", len(env_paths))

    return virtual_device.run_multi_device_configs(configs, env_paths)


if __name__ == "__main__":
    raise SystemExit(main())
