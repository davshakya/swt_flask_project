Place one `.env` file per virtual device in this directory.

Examples:

- `device-001.env`
- `device-002.env`
- `device-003.env`

When you run:

```powershell
python scripts/run_virtual_devices.py
```

the script checks `generated/` first, then this directory, and starts one emulator instance per `.env` file it finds.

Use [`device-template.env.example`](device-template.env.example) as the starting point for additional devices.

For automatic batch generation without starting the emulators yet, run:

```powershell
python scripts/generate_virtual_device_envs.py --base-url http://127.0.0.1:8000/ --device_count 10
```

That command writes generated env files under `generated/`.

To feed live data from those generated devices, run:

```powershell
python scripts/run_virtual_devices.py
```

If you want to force this directory instead of `generated/`, pass:

```powershell
python scripts/run_virtual_devices.py --env-dir tests/virtual_devices
```

If you deleted those device IDs from the local admin page earlier, generating or running them will restore them to the local admin device list.

If you still want the old one-step behavior, you can keep using:

```powershell
python scripts/run_virtual_devices.py --base-url http://127.0.0.1:8000/ --device_count 10
```
