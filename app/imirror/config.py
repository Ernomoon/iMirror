"""Persistent settings (%APPDATA%\\iMirror\\config.json)."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    # --- receiver / quality ---
    "uxplay_path": "",
    "server_name": "iMirror",
    "preset": "max4k",
    "width": 3840,
    "height": 2160,
    "refresh": 120,
    "max_fps": 120,
    "h265": True,
    "low_latency": True,
    "audio": True,
    "video_sink": "d3d12videosink",
    "software_decode": False,
    "always_on_top": False,
    "auto_fit": True,
    "pin": "",
    # --- control ---
    "serial_port": "",
    "control_mode": "capture",          # off | capture | tap
    "sensitivity": 1.0,
    "wheel_multiplier": 1.0,
    "invert_wheel": False,
    "swap_ctrl_cmd": True,
    "release_hotkey": "rctrl",          # rctrl | ralt | scroll | pause
    "report_interval_ms": 8,
    "calibration": {},
    "action_buttons": {                 # AssistiveTouch "Customize Additional Buttons"
        "home": "middle",
        "switcher": "back",
        "control_center": "forward",
        "notifications": "b6",
        "screenshot": "b7",
        "lock": "b8",
    },
    "flash_chip": "esp32",
    "transport": "",                    # ble (built-in Bluetooth) | esp32
    "forced_ip": "",                    # announce only on this adapter (empty = all LAN adapters)
    "theme": "dark",
    "reduce_motion": False,
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "iMirror"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = config_dir() / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if k in cfg and isinstance(cfg[k], dict) and isinstance(v, dict):
                    cfg[k].update(v)
                elif k in cfg:
                    cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    path = config_dir() / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
