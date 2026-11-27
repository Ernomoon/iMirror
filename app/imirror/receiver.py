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

# Fixed ports (UxPlay's documented legacy set, like iPhoneMirror): random ports are unsupported
# behind a firewall. (tcp: mirror, control, data; udp: timing, control, audio)
PORT_SETS = [
    ((7100, 7000, 7001), (6000, 6001, 7011)),
    ((7120, 7020, 7021), (6020, 6021, 7031)),
    ((7140, 7040, 7041), (6040, 6041, 7051)),
]
H264_PARSER = "h264parse config-interval=-1 disable-passthrough=true"
H264_SOFT_DECODER = ("avdec_h264 output-corrupt=false discard-corrupted-frames=true "
                     "automatic-request-sync-points=true")


def _ports_free(tcp, udp) -> bool:
    import socket
    for kind, ports in ((socket.SOCK_STREAM, tcp), (socket.SOCK_DGRAM, udp)):
        for p in ports:
            s = socket.socket(socket.AF_INET, kind)
            try:
                s.bind(("0.0.0.0", p))
            except OSError as e:
                import logging
                logging.getLogger("imirror").info("port %s/%s busy: %s", "tcp" if kind == socket.SOCK_STREAM else "udp", p, e)
                return False
            finally:
                s.close()
    return True


def pick_ports():
    """First fixed port set that is free, else None (UxPlay picks random ports)."""
    for tcp, udp in PORT_SETS:
        if _ports_free(tcp, udp):
            return tcp, udp
    return None


def safe_name(name: str) -> str:
    """UxPlay's Windows argument parser splits on whitespace: use one DNS-SD-safe token."""
    name = re.sub(r"\s+", "-", (name or "").strip())
    name = re.sub(r"[^\w\-.()\[\]]", "", name, flags=re.UNICODE).strip("-.")
    return name[:48] or "iMirror"


def device_id(cfg: dict) -> str:
    v = str(cfg.get("device_id") or "")
    if re.fullmatch(r"02(:[0-9a-f]{2}){5}", v):
        return v
    import secrets
    v = "02:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(5))
    cfg["device_id"] = v
    return v


def build_args(cfg: dict, exe: str) -> list[str]:
    w = int(cfg["width"]); h = int(cfg["height"])
    r = int(cfg["refresh"]); fps = int(cfg["max_fps"])
    if not (1 <= w <= 9999 and 1 <= h <= 9999):
        raise ValueError("Разрешение: от 1 до 9999 по каждой стороне")
    if not (1 <= r <= 255 and 1 <= fps <= 255):
        raise ValueError("Частота и FPS: от 1 до 255 (ограничение протокола UxPlay)")
    name = safe_name(cfg.get("server_name") or "iMirror")
    args = [exe, "-n", name, "-nh", "-m", device_id(cfg)]
    ports = cfg.get("_ports", "auto")
    if ports == "auto":
        ports = pick_ports()
    if ports:
        tcp, udp = ports
        args += ["-p", "tcp", ",".join(map(str, tcp)), "-p", "udp", ",".join(map(str, udp))]
    if cfg.get("_minimal"):
        # Plain UxPlay defaults: nothing that could trip a particular PC.
        return args + ["-as", "0", "-vs", "autovideosink"]
    args += ["-s", f"{w}x{h}@{r}", "-fps", str(fps), "-nohold", "-nofreeze", "-vp", H264_PARSER]
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
        args += ["-avdec", "-vp", H264_PARSER, "-vd", H264_SOFT_DECODER]
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
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("_MEI", "_PYI", "PYTHON", "TCL_", "TK_", "QT_", "WEBVIEW2", "GST_", "GIO_", "GTK_"))}
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
        # One plugin cache per receiver build: a cache written by another GStreamer (e.g. an old
        # MSYS2 UxPlay) made the bundled one load foreign plugins and crash at start-up.
        try:
            st = Path(exe).stat()
            tag = f"{st.st_size:x}-{int(st.st_mtime):x}"
        except OSError:
            tag = "x"
        reg = cache / f"gst-registry-{tag}.bin"
        for old in cache.glob("gst-registry*.bin"):
            if old != reg:
                try:
                    old.unlink()
                except OSError:
                    pass
        env["GST_REGISTRY_1_0"] = str(reg)
        env["GST_REGISTRY_FORK"] = "yes"
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


def output_log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "iMirror"
    base.mkdir(parents=True, exist_ok=True)
    for i in range(4):                 # an old receiver may still hold the file for a moment
        p = base / ("uxplay-output.log" if i == 0 else f"uxplay-output-{i}.log")
        try:
            with open(p, "wb"):
                return p
        except OSError:
            continue
    return base / f"uxplay-output-{os.getpid()}.log"


def _is_imirror_build(p: Path) -> bool:
    """Only UxPlay built with iMirror's patch can work with iMirror (external mDNS, stats)."""
    try:
        return b"IMIRROR_MDNS" in p.read_bytes()
    except OSError:
        return False


def find_uxplay(cfg: dict) -> str | None:
    """The receiver bundled with iMirror always wins. An old UxPlay elsewhere (for example the
    one iMirror 1.x built in MSYS2) lacks the patch and would never announce itself."""
    here = Path(__file__).resolve().parent.parent.parent
    candidates = [bundle_root() / "bin" / "uxplay.exe", here / "receiver" / "bin" / "uxplay.exe"]
    if cfg.get("uxplay_path"):
        candidates.append(Path(cfg["uxplay_path"]))
    candidates.append(Path(os.environ.get("IMIRROR_MSYS", r"C:\msys64")) / "ucrt64" / "bin" / "uxplay.exe")
    if sys.platform != "win32":
        candidates.append(Path("/usr/local/bin/uxplay"))
    for c in candidates:
        if c.is_file() and _is_imirror_build(c):
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
        self.tail = deque(maxlen=40)          # last output lines (shown when UxPlay stops or hangs)
        self.stage = ""                       # "", "register", "ready"
        self.stopping = False                 # stop() was requested by iMirror itself
        self.started_at = 0.0
        self._lock = threading.Lock()
        self._starting = False

    @property
    def running(self) -> bool:
        return self._starting or (self.proc is not None and self.proc.poll() is None)

    @property
    def pid(self) -> int | None:
        """PID of uxplay itself (on Windows the launcher is cmd.exe)."""
        if not self.running or self.proc is None:
            return None
        if sys.platform != "win32":
            return self.proc.pid
        if not getattr(self, "_uxpid", None):
            self._uxpid = _child_pid(self.proc.pid)
        return self._uxpid

    def start(self, cfg: dict):
        """Start UxPlay. Concurrent calls (autostart + a click) never start a second process."""
        with self._lock:
            if self.running:
                return
            self._starting = True
        try:
            self._start(cfg)
        finally:
            self._starting = False

    def _start(self, cfg: dict):
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
        self.tail.clear()
        self.stage = ""
        self.stopping = False
        import time as _t
        self.started_at = _t.monotonic()
        self.on_event("log", f"Запуск {exe}: " + " ".join(args[1:]))
        # Output goes to a file that iMirror tails, not to a pipe: on some PCs UxPlay stops
        # dead on its first write after the network sockets open when stdout is a pipe from
        # iMirror, while the same binary runs fine with a file (verified on the user's PC).
        dll_dir = None
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            # PyInstaller points the DLL search path at iMirror's own folder; children inherit it,
            # so UxPlay could pick up iMirror's copies of shared DLLs instead of its own.
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                b = ctypes.create_unicode_buffer(1024)
                if k32.GetDllDirectoryW(1024, b):
                    dll_dir = b.value
                k32.SetDllDirectoryW(None)
            except Exception:
                pass
        out_path = output_log_path()
        cwd = str(Path(exe).resolve().parent)
        try:
            if sys.platform == "win32":
                # Launch exactly the way that works on the affected PC: through cmd.exe with the
                # output redirected to a file, no priority change, no job object.
                line = subprocess.list2cmdline(args) + f' > "{out_path}" 2>&1 < NUL'
                self.proc = subprocess.Popen(
                    f'"{os.environ.get("ComSpec", "cmd.exe")}" /d /s /c "{line}"',
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env, creationflags=flags, cwd=cwd)
            else:
                with open(out_path, "wb") as out:
                    self.proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT,
                                                 stdin=subprocess.DEVNULL, env=env, cwd=cwd)
        finally:
            if dll_dir:
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetDllDirectoryW(dll_dir)
                except Exception:
                    pass
        self.out_path = out_path
        self._uxpid = None
        self._reader = threading.Thread(target=self._read, args=(self.proc,), daemon=True)
        self._reader.start()
        self.on_event("state", "running")

    def _read(self, proc: subprocess.Popen):
        import time as _t
        buf = b""
        stream = open(self.out_path, "rb")
        while True:
            chunk = stream.read(65536)
            if not chunk:
                if proc.poll() is not None:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                else:
                    _t.sleep(0.03)
                    continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                if proc is self.proc:
                    self.tail.append(line[:300])
                try:
                    self._line(line)
                except Exception:          # never let one bad line stop reading (the pipe would fill up)
                    import logging
                    logging.getLogger("imirror").exception("receiver line %r", line[:200])
        stream.close()
        code = proc.wait()
        if proc is self.proc:
            self.info = StreamInfo()
            self.on_event("state", f"stopped:{code}")

    def _line(self, line: str):
        if line.startswith("IMIRROR_STAGE "):
            self.stage = line.split(" ", 1)[1].strip()
            self.on_event("stage", self.stage)
            return
        if self.line_hook and self.line_hook(line):
            if line.startswith("IMIRROR_MDNS"):
                self.stage = "ready"
            return
        kind = parse_line(line, self.info)
        if kind:
            self.on_event(kind, self.info)
        elif line.strip():
            self.on_event("log", line)

    def stop(self):
        self.stopping = True
        proc = self.proc
        if proc and proc.poll() is None and sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            return
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def _child_pid(ppid: int) -> int | None:
    import ctypes
    from ctypes import wintypes as W

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                    ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", W.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.WinDLL("kernel32")
    k32.CreateToolhelp32Snapshot.restype = W.HANDLE
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    if not snap or snap == W.HANDLE(-1).value:
        return None
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ParentProcessID == ppid and e.szExeFile.lower().startswith("uxplay"):
                return int(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return None


_JOB = None


def _kill_with_parent(proc):
    """Put UxPlay in a job object that closes with iMirror: no orphaned receiver holding the
    AirPlay ports if iMirror crashes or is killed."""
    global _JOB
    try:
        import ctypes
        from ctypes import wintypes as W
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = W.HANDLE
        k32.OpenProcess.restype = W.HANDLE
        k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        k32.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
        k32.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
        k32.CloseHandle.argtypes = [W.HANDLE]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        if _JOB is None:
            job = k32.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return
            _JOB = job
        h = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)      # PROCESS_TERMINATE | SET_QUOTA
        if h:
            k32.AssignProcessToJobObject(_JOB, h)
            k32.CloseHandle(h)
    except Exception:
        pass


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
