Place one `.env` file per virtual device in this directory.

Examples:

- `device-001.env`
- `device-002.env`
- `device-003.env`

When you run:

```powershell
python scripts/virtual_device.py
```

the script scans this directory and starts one emulator instance for each `.env` file it finds.

Use [`device-template.env.example`](device-template.env.example) as the starting point for additional devices.
