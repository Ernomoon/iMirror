"""USB-serial link to the ESP32 and the HID command scheduler."""
from __future__ import annotations

import queue
import threading
import time

from . import protocol as P
from .calib import Calibration, DEFAULT_HOME_COUNTS

KNOWN_VIDS = {0x10C4, 0x1A86, 0x303A, 0x0403, 0x067B}   # CP210x, CH34x, Espressif, FTDI, Prolific


class SerialLink:
    """Owns the COM port: open/auto-reconnect, reads status lines, writes frames."""

    def __init__(self, on_event):
        self.on_event = on_event          # on_event(kind, payload) from background threads
        self.port_name = ""
        self._ser = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.fw_info = ""
        self.phone_connected = False

    # -- public -------------------------------------------------------------------------
    @staticmethod
    def list_ports():
        try:
            from serial.tools import list_ports
        except ImportError:
            return []
        ports = []
        for p in list_ports.comports():
            likely = p.vid in KNOWN_VIDS if p.vid is not None else False
            ports.append((p.device, f"{p.device} — {p.description}", likely))
        ports.sort(key=lambda t: (not t[2], t[0]))
        return ports

    @property
    def connected(self) -> bool:
        return self._ser is not None and bool(self.fw_info)

    def start(self, port_name: str):
        self.stop()
        self.port_name = port_name
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2)
        self._thread = None
        self._close()

    def write(self, data: bytes) -> bool:
        with self._lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write(data)
                return True
            except Exception as e:     # serial.SerialException / OSError
                self.on_event("log", f"ESP32: ошибка записи: {e}")
                self._close_locked()
                return False

    # -- internals ------------------------------------------------------------------------
    def _open(self):
        import serial
        s = serial.Serial()
        s.port = self.port_name
        s.baudrate = 115200
        s.timeout = 0.05
        s.write_timeout = 0.5
        # Keep DTR/RTS released so opening the port does not reset classic ESP32 boards.
        s.dtr = False
        s.rts = False
        s.open()
        return s

    def _close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        was = bool(self.fw_info)
        self._ser = None
        self.fw_info = ""
        self.phone_connected = False
        if was:
            self.on_event("esp", "closed")

    def _run(self):
        backoff = 0.5
        while not self._stop.is_set():
            try:
                ser = self._open()
            except Exception as e:
                self.on_event("esp", f"error:{e}")
                if self._stop.wait(min(backoff, 3.0)):
                    return
                backoff *= 2
                continue
            backoff = 0.5
            with self._lock:
                self._ser = ser
            self._handshake_and_read(ser)
            self._close()
            if not self._stop.wait(1.0):
                self.on_event("log", "ESP32: переподключение…")

    def _handshake_and_read(self, ser):
        buf = b""
        last_ping = 0.0
        started = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if not self.fw_info and now - last_ping > 0.7:
                if now - started > 6.0:
                    self.on_event("esp", "error:нет ответа от прошивки iMirror (прошита ли плата?)")
                    return
                if not self.write(P.ping()):
                    return
                last_ping = now
            try:
                chunk = ser.read(256)
            except Exception as e:
                self.on_event("log", f"ESP32: порт закрыт ({e})")
                return
            if self._ser is not ser:
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._line(raw.decode("utf-8", errors="replace").strip())
            if len(buf) > 4096:
                buf = b""

    def _line(self, line: str):
        if line.startswith("IMIRROR_FW"):
            first = not self.fw_info
            self.fw_info = line
            self.phone_connected = "connected=1" in line
            if first:
                self.on_event("esp", "ready")
            self.on_event("phone", self.phone_connected)
        elif line.startswith("IMIRROR_EVT"):
            evt = line.split(" ", 1)[1] if " " in line else ""
            if evt in ("connected", "disconnected"):
                self.phone_connected = evt == "connected"
                self.on_event("phone", self.phone_connected)
            else:
                self.on_event("log", "ESP32: " + evt)
        elif line:
            self.on_event("log", "ESP32: " + line)


class Controller:
    """Single ordered command stream to the ESP32.

    All public methods are thread-safe and non-blocking (they only enqueue). The worker
    thread merges mouse movement and flushes it every few milliseconds so that a 1000 Hz
    gaming mouse never floods the 115200-baud serial link, while clicks/keys always keep
    their exact order relative to movement.
    """

    FLUSH_S = 0.004

    def __init__(self, link: SerialLink):
        self.link = link
        self.q: queue.Queue = queue.Queue()
        self.calibration = Calibration()
        self.src = (0, 0)                 # current stream size (w, h)
        self._acc_x = self._acc_y = 0.0
        self._acc_w = self._acc_p = 0.0
        self._last_flush = 0.0
        self._est = None                  # estimated pointer position in stream px (tap mode)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---------------- enqueue API ----------------
    def move(self, dx: float, dy: float):
        self.q.put(("move", dx, dy))

    def scroll(self, wheel: float, pan: float = 0.0):
        self.q.put(("scroll", wheel, pan))

    def buttons(self, mask: int):
        self.q.put(("buttons", mask))

    def keys(self, modifiers: int, usages):
        self.q.put(("keys", modifiers, list(usages)))

    def consumer_tap(self, usage: int):
        self.q.put(("raw", P.consumer(usage) + P.delay(60) + P.consumer(0)))

    def click_buttons(self, mask: int, hold_ms: int = 60):
        self.q.put(("raw", P.buttons(mask) + P.delay(hold_ms) + P.buttons(0)))

    def key_combo(self, modifiers: int, usages=(), hold_ms: int = 50):
        self.q.put(("raw", P.keys(modifiers, usages) + P.delay(hold_ms) + P.keys(0, [])))

    def type_text(self, strokes):
        data = bytearray()
        for mod, usage in strokes:
            data += P.keys(mod, [usage]) + P.keys(0, [])
        self.q.put(("raw", bytes(data)))

    def release_all(self):
        self.q.put(("release",))

    def raw(self, data: bytes):
        self.q.put(("raw", data))

    # tap-mode (absolute) API — coordinates in stream pixels
    def tap_down(self, sx: float, sy: float, mask: int = P.BTN_LEFT):
        self.q.put(("goto", sx, sy))
        self.q.put(("buttons", mask))

    def drag_px(self, dx_px: float, dy_px: float):
        self.q.put(("drag", dx_px, dy_px))

    def long_press(self, sx: float, sy: float, hold_ms: int = 650):
        self.q.put(("goto", sx, sy))
        self.q.put(("raw", P.buttons(P.BTN_LEFT) + P.delay(hold_ms) + P.buttons(0)))

    def scroll_at(self, sx: float, sy: float, wheel: float, pan: float = 0.0):
        self.q.put(("goto_if_far", sx, sy))
        self.q.put(("scroll", wheel, pan))

    def point_at(self, sx: float, sy: float):
        """Move the iPhone pointer to a spot without tapping (calibration check)."""
        self.q.put(("goto", sx, sy))

    def calib_probe(self, counts: int, step: int):
        self.q.put(("probe", counts, step))

    def forget_position(self):
        self.q.put(("forget",))

    # ---------------- worker ----------------
    def _run(self):
        while True:
            timeout = self.FLUSH_S if (self._acc_x or self._acc_y or self._acc_w or self._acc_p) else 0.25
            try:
                item = self.q.get(timeout=timeout)
            except queue.Empty:
                self._flush_move()
                continue
            try:
                self._handle(item)
            except Exception as e:     # never let the worker die
                self.link.on_event("log", f"Ошибка управления: {e!r}")

    def _handle(self, item):
        kind = item[0]
        if kind == "move":
            self._acc_x += item[1]
            self._acc_y += item[2]
            if time.monotonic() - self._last_flush >= self.FLUSH_S:
                self._flush_move()
            return
        if kind == "scroll":
            self._acc_w += item[1]
            self._acc_p += item[2]
            self._flush_move()
            return
        # every other command is a barrier: flush movement first to keep the order
        self._flush_move()
        if kind == "buttons":
            self.link.write(P.buttons(item[1]))
        elif kind == "keys":
            self.link.write(P.keys(item[1], item[2]))
        elif kind == "raw":
            self.link.write(item[1])
        elif kind == "release":
            self._acc_x = self._acc_y = self._acc_w = self._acc_p = 0.0
            self.link.write(P.clear_move() + P.release())
        elif kind == "goto":
            self._goto(item[1], item[2], force_home=True)
        elif kind == "goto_if_far":
            self._goto(item[1], item[2], force_home=False)
        elif kind == "drag":
            self._drag(item[1], item[2])
        elif kind == "probe":
            counts, step = item[1], item[2]
            self.link.write(P.move(-DEFAULT_HOME_COUNTS, -DEFAULT_HOME_COUNTS, step=127)
                            + P.delay(120) + P.move(counts, counts, step=step))
            self._est = None
        elif kind == "forget":
            self._est = None

    def _flush_move(self):
        ix, iy = int(self._acc_x), int(self._acc_y)
        iw, ip = int(self._acc_w), int(self._acc_p)
        self._last_flush = time.monotonic()
        if ix or iy or iw or ip:
            self._acc_x -= ix
            self._acc_y -= iy
            self._acc_w -= iw
            self._acc_p -= ip
            self.link.write(P.move(ix, iy, iw, ip, 127))

    # absolute positioning helpers (stream pixels)
    def _goto(self, sx: float, sy: float, force_home: bool):
        cal = self.calibration
        w, h = self.src
        if not cal.valid or w <= 0 or h <= 0:
            return
        sx = min(max(sx, 0.0), w - 1.0)
        sy = min(max(sy, 0.0), h - 1.0)
        if not force_home and self._est is not None:
            ex, ey = self._est
            if abs(ex - sx) < 0.02 * w and abs(ey - sy) < 0.02 * h:
                return
        home = cal.home_counts(w, h)
        cx, cy = cal.counts_for(sx, sy, w, h)
        self.link.write(P.move(-home, -home, step=127) + P.delay(40)
                        + P.move(round(cx), round(cy), step=cal.step))
        self._est = (sx, sy)

    def _drag(self, dx_px: float, dy_px: float):
        cal = self.calibration
        w, h = self.src
        if not cal.valid or w <= 0 or self._est is None:
            return
        ex, ey = self._est
        nx = min(max(ex + dx_px, 0.0), w - 1.0)
        ny = min(max(ey + dy_px, 0.0), h - 1.0)
        # convert the *absolute* target into counts relative to the last sent position,
        # so rounding errors never accumulate during a long swipe
        cx0, cy0 = cal.counts_for(ex, ey, w, h)
        cx1, cy1 = cal.counts_for(nx, ny, w, h)
        mx, my = round(cx1) - round(cx0), round(cy1) - round(cy0)
        if mx or my:
            self.link.write(P.move(mx, my, step=cal.step))
        self._est = (nx, ny)
