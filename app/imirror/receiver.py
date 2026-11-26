"""Runs the (patched) UxPlay AirPlay receiver and parses its statistics."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

PRESETS = {
    # key: (title, width, height, refresh, fps, h265)
    "max4k": ("Максимум: 4K H.265, запрос 120 Гц", 3840, 2160, 120, 120, True),
    "1440p": ("1440p H.265, запрос 120 Гц", 2560, 1440, 120, 120, True),
    "1080p": ("1080p H.264 (совместимость, старые iPhone)", 1920, 1080, 120, 120, False),
    "custom": ("Свои значения", None, None, None, None, None),
}

VIDEO_SINKS = ["d3d12videosink", "d3d11videosink", "autovideosink"]


def build_args(cfg: dict, exe: str) -> list[str]:
    w = int(cfg["width"]); h = int(cfg["height"])
    r = int(cfg["refresh"]); fps = int(cfg["max_fps"])
    if not (1 <= w <= 9999 and 1 <= h <= 9999):
        raise ValueError("Разрешение: от 1 до 9999 по каждой стороне")
    if not (1 <= r <= 255 and 1 <= fps <= 255):
        raise ValueError("Частота и FPS: от 1 до 255 (ограничение протокола UxPlay)")
    name = (cfg.get("server_name") or "iMirror").strip() or "iMirror"
    args = [exe, "-n", name, "-nh", "-s", f"{w}x{h}@{r}", "-fps", str(fps), "-nohold"]
    if cfg.get("h265"):
        args.append("-h265")
    if cfg.get("low_latency"):
        args += ["-vsync", "no"]
    if cfg.get("audio"):
        args += ["-as", "wasapisink"]
    else:
        args += ["-as", "0"]
    sink = pick_sink(exe, cfg.get("video_sink") or "d3d12videosink")
    args += ["-vs", sink]
    if cfg.get("software_decode"):
        args.append("-avdec")
    pin = str(cfg.get("pin") or "").strip()
    if pin:
        if not re.fullmatch(r"\d{4}", pin) or pin == "0000":
            raise ValueError("PIN: 4 цифры, от 0001 до 9999")
        args += ["-pin", pin]
    return args


def bundle_root() -> Path:
    """Receiver shipped by the installer: <install dir>/receiver/{bin,lib,libexec}."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "receiver"
    return Path(__file__).resolve().parent.parent.parent / "dist-receiver"


def receiver_env(exe: str) -> dict:
    """Environment for uxplay: bundled GStreamer is isolated from any other GStreamer install."""
    env = os.environ.copy()
    bindir = Path(exe).resolve().parent
    root = bindir.parent
    plugins = root / "lib" / "gstreamer-1.0"
    if plugins.is_dir():
        env["PATH"] = os.pathsep.join([str(bindir), os.environ.get("SystemRoot", r"C:\Windows") + r"\System32",
                                       env.get("PATH", "")])
        env["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(plugins)
        env["GST_PLUGIN_SYSTEM_PATH"] = str(plugins)
        env.pop("GST_PLUGIN_PATH", None)
        env.pop("GST_PLUGIN_PATH_1_0", None)
        scanner = root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"
        if scanner.is_file():
            env["GST_PLUGIN_SCANNER_1_0"] = str(scanner)
            env["GST_PLUGIN_SCANNER"] = str(scanner)
        cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "iMirror"
        cache.mkdir(parents=True, exist_ok=True)
        env["GST_REGISTRY_1_0"] = str(cache / "gst-registry.bin")
    else:
        msys_bin = str(Path(os.environ.get("IMIRROR_MSYS", r"C:\msys64")) / "ucrt64" / "bin")
        env["PATH"] = os.pathsep.join([str(bindir), msys_bin, env.get("PATH", "")])
    env["IMIRROR_EXTERNAL_MDNS"] = "1"
    return env


_SINK_CACHE: dict = {}


def pick_sink(exe: str, wanted: str) -> str:
    """Use the wanted video sink if this GStreamer has it, else the best available one."""
    inspect = Path(exe).resolve().parent / "gst-inspect-1.0.exe"
    if sys.platform != "win32" or not inspect.is_file():
        return wanted

    def has(el):
        if el not in _SINK_CACHE:
            try:
                r = subprocess.run([str(inspect), el], env=receiver_env(exe), capture_output=True, timeout=20,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                _SINK_CACHE[el] = r.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                _SINK_CACHE[el] = True       # unknown: don't block the user's choice
        return _SINK_CACHE[el]

    for el in [wanted] + [s for s in VIDEO_SINKS if s != wanted]:
        if has(el):
            return el
    return "autovideosink"


def find_uxplay(cfg: dict) -> str | None:
    candidates = []
    if cfg.get("uxplay_path"):
        candidates.append(Path(cfg["uxplay_path"]))
    here = Path(__file__).resolve().parent.parent.parent
    candidates += [
        bundle_root() / "bin" / "uxplay.exe",
        here / "receiver" / "bin" / "uxplay.exe",
        Path(os.environ.get("IMIRROR_MSYS", r"C:\msys64")) / "ucrt64" / "bin" / "uxplay.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


_STATS = re.compile(r"IMIRROR_STATS fps=([\d.]+) kbps=([\d.]+) frames=(\d+)")
_VIDEO = re.compile(r"IMIRROR_VIDEO codec=(\w+) w=(\d+) h=(\d+)")
_CONN = re.compile(r"IMIRROR_CONN open=(\d+)")


@dataclass
class StreamInfo:
    connections: int = 0
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    kbps: float = 0.0
    fps_history: deque = field(default_factory=lambda: deque(maxlen=10))

    @property
    def peak_fps(self) -> float:
        return max(self.fps_history) if self.fps_history else 0.0


def parse_line(line: str, info: StreamInfo) -> str | None:
    """Update info from one output line. Returns event name if something changed."""
    m = _STATS.search(line)
    if m:
        info.fps = float(m.group(1))
        info.kbps = float(m.group(2))
        info.fps_history.append(info.fps)
        return "stats"
    m = _VIDEO.search(line)
    if m:
        info.codec, info.width, info.height = m.group(1), int(m.group(2)), int(m.group(3))
        return "video"
    m = _CONN.search(line)
    if m:
        info.connections = int(m.group(1))
        if info.connections == 0:
            info.fps = 0.0
            info.kbps = 0.0
            info.fps_history.clear()
        return "conn"
    return None


class Receiver:
    def __init__(self, on_event, line_hook=None):
        """on_event(kind, payload) is called from a background thread.
        line_hook(line) -> bool may consume a line (e.g. IMIRROR_MDNS)."""
        self.on_event = on_event
        self.line_hook = line_hook
        self.proc: subprocess.Popen | None = None
        self.info = StreamInfo()
        self._reader: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.running else None

    def start(self, cfg: dict):
        if self.running:
            return
        exe = find_uxplay(cfg)
        if not exe:
            raise FileNotFoundError("uxplay.exe не найден. Запустите install.ps1 (он собирает приёмник) "
                                    "или укажите путь в настройках.")
        args = build_args(cfg, exe)
        env = receiver_env(exe)
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        self.info = StreamInfo()
        self.on_event("log", "Запуск: " + " ".join(args[1:]))
        self.proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, creationflags=flags)
        if sys.platform == "win32":
            _raise_priority(self.proc)
        self._reader = threading.Thread(target=self._read, args=(self.proc,), daemon=True)
        self._reader.start()
        self.on_event("state", "running")

    def _read(self, proc: subprocess.Popen):
        buf = b""
        stream = proc.stdout
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                if self.line_hook and self.line_hook(line):
                    continue
                kind = parse_line(line, self.info)
                if kind:
                    self.on_event(kind, self.info)
                elif line.strip():
                    self.on_event("log", line)
        code = proc.wait()
        if proc is self.proc:
            self.info = StreamInfo()
            self.on_event("state", f"stopped:{code}")

    def stop(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def _raise_priority(proc):
    """Give the video pipeline ABOVE_NORMAL priority: fewer dropped frames when the PC is busy."""
    try:
        import ctypes
        PROCESS_SET_INFORMATION = 0x0200
        ABOVE_NORMAL = 0x00008000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(PROCESS_SET_INFORMATION, False, proc.pid)
        if h:
            k32.SetPriorityClass(h, ABOVE_NORMAL)
            k32.CloseHandle(h)
    except Exception:
        pass
