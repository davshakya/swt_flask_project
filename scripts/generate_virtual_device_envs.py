#!/usr/bin/env python
"""Generate virtual device env files without starting the emulators."""

from __future__ import annotations

import logging

import run_virtual_devices as virtual_device


def main() -> int:
    parser = virtual_device.build_parser()
    parser.description = "Generate numbered virtual device env files without starting the emulators."
    args = parser.parse_args()

    if args.device_count <= 0:
        raise SystemExit("--device_count must be greater than 0. Example: --device_count 10")

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    virtual_device.generate_virtual_device_env_files(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
