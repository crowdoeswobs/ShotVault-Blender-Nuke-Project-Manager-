# ShotMgr — Build Instructions

## Requirements

- Python 3.10+ (64-bit, matching your target Windows version)
- pip

## Setup & Build

```bash
# 1. Install dependencies
pip install flask pyinstaller

# 2. Build the folder distribution
pyinstaller shotvault.spec

# 3. Your app is ready at:
#    dist/ShotMgr/ShotMgr.exe
```

## Running during development (no build needed)

```bash
pip install flask
python server.py
```

This opens the app in your default browser automatically.

## What the build produces

```
dist/
  ShotMgr/
    ShotMgr.exe        ← double-click to launch
    shotvault_config.json  ← auto-created on first run (stores settings)
    _internal/         ← bundled Python runtime + Flask (don't delete)
```

Copy the entire `ShotMgr/` folder anywhere you like — USB drive, network share, etc.

## First run checklist

1. Double-click `ShotMgr.exe` — your browser opens automatically
2. Click the gear icon (top-right) → Settings
3. Set the full path to your `blender.exe`, e.g.:
   `C:\Program Files\Blender Foundation\Blender 4.3\blender.exe`
4. Click the folder bar at the top → pick your project root
5. ShotMgr scans and displays your shots — click any `.blend` row to launch it

## Expected folder structure

```
project_root/
  shots/              ← ShotMgr looks for this subfolder first
    010/              ← sequence
      0010/           ← shot
        work/
          cg/
            scene_v001.blend
      0020/
        ...
    020/
      ...
```

If there's no `shots/` subfolder, ShotMgr treats the root itself as the shot container.

## Keyboard shortcuts

| Key          | Action           |
|-------------|------------------|
| `/`          | Focus search     |
| `Ctrl+R`     | Rescan           |
| `Ctrl+,`     | Open settings    |
| `Esc`        | Close settings   |

## Adding a custom icon

Place a `shotvault.ico` file next to `shotvault.spec`, then edit the spec:
```python
icon='shotvault.ico',
```
Re-run `pyinstaller shotvault.spec`.
