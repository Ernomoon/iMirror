"""Persistent settings (%APPDATA%\\iMirror\\config.json)."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
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
    "forced_ip": "",          # announce only on this adapter (empty = automatic)
    "wifi_boost": True,       # Wi-Fi media-streaming mode while mirroring
    "reduce_motion": False,
    "autostart": True,        # start the receiver when iMirror opens
    "device_id": "",          # stable AirPlay device id (02:xx:..), generated once
    "control_enabled": False, # Bluetooth control of the iPhone (turned on from the UI)
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
                if k in cfg:
                    cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    path = config_dir() / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
