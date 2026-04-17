# Virtual Device Fleet Directory

This directory holds one `.env` file per emulated MCU used by the Flask-side virtual device runner.

The runner documented in [`../../README.md`](../../README.md) and implemented by `scripts/run_virtual_devices.py` uses these files to simulate:

- telemetry posts to `/status`
- command polling from `/device/command`
- command acknowledgements to `/device/command/ack`
- source tank and relay connectivity behavior

## File Layout

Recommended hand-managed files:

- `device-001.env`
- `device-002.env`
- `device-003.env`

Starter templates:

- `device-template.env.example`
- `../virtual_device.env.example` for legacy single-device fallback mode

Generated batches are usually written under `generated/`.

## Discovery Rules

Default runner behavior:

1. look in `tests/virtual_devices/generated/`
2. if nothing is selected there, look in `tests/virtual_devices/`
3. start one emulator instance per `.env` file discovered

You can override discovery with:

```powershell
python scripts/run_virtual_devices.py --env-dir tests/virtual_devices
python scripts/run_virtual_devices.py --env-file tests/virtual_devices/device-001.env --env-file tests/virtual_devices/device-003.env
```

If you pass explicit single-device CLI flags such as `--device-id` or `--device-key`, the runner switches back to classic single-device mode instead of scanning this folder.

## Typical Workflows

### Generate a fleet without starting it

```powershell
python scripts/generate_virtual_device_envs.py --base-url http://127.0.0.1:8000/ --device_count 10
```

### Run the discovered fleet

```powershell
python scripts/run_virtual_devices.py
```

### Generate and run in one step

```powershell
python scripts/run_virtual_devices.py --base-url http://127.0.0.1:8000/ --device_count 10
```

## Integration Notes

- Keep Flask on `SWT_DEVICE_SOURCE_MODE=virtual` when you want the virtual fleet to be the active data source.
- Local Flask development usually auto-registers IDs from these env files when `SEED_VIRTUAL_DEVICE_ENVS=true`.
- Render/cloud deployments default to ignoring and purging these repo-scoped virtual-device records on boot.
- The runner stays local by default: it uses `SWT_VIRTUAL_DEVICE_BASE_URL`, then `SWT_LOCAL_FLASK_BASE_URL`, then `http://127.0.0.1:8000/`. It does not fall back to `SWT_CLOUD_BASE_URL`.
- Use `SWT_VIRTUAL_DEVICE_TIME_SCALE` in a shared or per-device env file if you want the tank simulation to evolve faster than real time.

## Related Files

- [`../../README.md`](../../README.md)
- [`device-template.env.example`](device-template.env.example)
- [`../virtual_device.env.example`](../virtual_device.env.example)
