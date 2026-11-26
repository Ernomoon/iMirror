"""iMirror application core: everything except the window.

The UI (webui.py + ui/*) only reads `Core.state()` snapshots and calls `Core.dispatch()`.
All background threads report through `Core._event`, which updates state and asks the UI
to refresh (throttled).
"""
from __future__ import annotations

import collections
import ctypes
import logging
import sys
import threading
import time

from . import config as C
from . import protocol as P
from . import keymap
from . import flasher
from .bridge import SerialLink, Controller
from .calib import Calibration, CalibrationSession, CalibrationError, screen_to_stream
from .mdns import Advertiser, list_adapters
from .receiver import Receiver, PRESETS, VIDEO_SINKS
from . import winble

IS_WIN = sys.platform == "win32"
if IS_WIN:
    from . import win32, mirrorwin
    from .capture import InputEngine, HOTKEYS
else:  # development on other OSes: UI works, Windows-only parts are inert
    win32 = mirrorwin = InputEngine = None
    HOTKEYS = {"rctrl": (0, "Правый Ctrl"), "ralt": (0, "Правый Alt"),
               "scroll": (0, "Scroll Lock"), "pause": (0, "Pause/Break")}

BUTTON_BITS = {"middle": P.BTN_MIDDLE, "back": P.BTN_BACK, "forward": P.BTN_FORWARD,
               "b6": P.BTN_6, "b7": P.BTN_7, "b8": P.BTN_8}
ACTIONS = ["home", "switcher", "control_center", "notifications", "screenshot", "lock"]


class Core:
    def __init__(self, on_change=None):
        self.cfg = C.load()
        if not self.cfg.get("transport"):
            self.cfg["transport"] = "ble" if winble.available() else "esp32"
        self.on_change = on_change or (lambda: None)
        self.log = collections.deque(maxlen=400)
        self.toasts = collections.deque(maxlen=20)
        self._toast_id = 0
        self._lock = threading.RLock()

        self.advertiser = Advertiser(self._event_mdns)
        self.advertiser.forced_ip = self.cfg.get("forced_ip", "")
        self.mdns = {"ok": None, "addresses": [], "error": ""}
        self.receiver = Receiver(lambda k, v: self._event("rx", k, v), line_hook=self.advertiser.feed)
        self.hid = None
        self.ctl = Controller(_LinkProxy(self))
        self.ctl.calibration = Calibration.from_dict(self.cfg.get("calibration") or {})
        self.hid_error = ""
        self.flashing = False

        self.mirror = (None, 0, 0)
        self.mirror_hwnd = None
        self.fullscreen = mirrorwin.FullscreenToggler() if IS_WIN else None
        self.captured = False
        self.calib = None
        self.calib_step = 0
        self.calib_report = ""
        self.fps_hist = collections.deque([0.0] * 60, maxlen=60)

        self.engine = None
        if IS_WIN:
            self.engine = InputEngine(self.ctl, lambda: self.mirror,
                                      lambda k, v: self._event("in", k, v), self.cfg)
            self.engine.enabled = False
        self._stop = threading.Event()
        threading.Thread(target=self._tracker, name="iMirror-tracker", daemon=True).start()
        self.connect_hid(self.cfg["transport"], self.cfg.get("serial_port", ""), quiet=True)

    # ===================================================================== helpers
    def logln(self, text: str):
        self.log.append(time.strftime("%H:%M:%S ") + text)
        logging.getLogger("imirror").info(text)
        self.on_change()

    def toast(self, text: str, kind: str = "info"):
        self._toast_id += 1
        self.toasts.append({"id": self._toast_id, "text": text, "kind": kind})
        self.logln(text)

    def save(self):
        try:
            C.save(self.cfg)
        except OSError as e:
            self.log.append(f"Не удалось сохранить настройки: {e}")

    # ===================================================================== state
    def state(self) -> dict:
        info = self.receiver.info
        link = self.hid
        cal = self.ctl.calibration
        hk = HOTKEYS.get(self.cfg["release_hotkey"], HOTKEYS["rctrl"])[1]
        return {
            "platform": sys.platform,
            "cfg": {k: v for k, v in self.cfg.items() if k != "calibration"},
            "presets": {k: {"title": v[0], "w": v[1], "h": v[2], "hz": v[3], "fps": v[4], "h265": v[5]}
                        for k, v in PRESETS.items()},
            "sinks": VIDEO_SINKS,
            "hotkeys": {k: v[1] for k, v in HOTKEYS.items()},
            "hotkey_name": hk,
            "chips": flasher.CHIPS,
            "receiver": {
                "running": self.receiver.running,
                "connections": info.connections,
                "codec": info.codec, "width": info.width, "height": info.height,
                "fps": round(info.fps, 1), "peak": round(info.peak_fps, 1),
                "mbps": round(info.kbps / 1000.0, 1),
                "fps_hist": list(self.fps_hist),
                "window": bool(self.mirror_hwnd),
                "fullscreen": bool(self.fullscreen and self.mirror_hwnd and self.mirror_hwnd in self.fullscreen.saved),
            },
            "mdns": self.mdns,
            "hid": {
                "transport": self.cfg["transport"],
                "ble_available": winble.available(),
                "ready": bool(link and link.connected),
                "phone": bool(link and link.phone_connected),
                "info": link.fw_info if link else "",
                "error": self.hid_error,
                "port": self.cfg.get("serial_port", ""),
                "flashing": self.flashing,
            },
            "control": {
                "mode": self.cfg["control_mode"],
                "captured": self.captured,
                "calibrated": cal.valid,
                "calib_fast": cal.valid and cal.step == 127,
                "calib_active": self.calib is not None,
                "calib_step": self.calib_step,
                "calib_report": self.calib_report,
            },
            "log": list(self.log)[-200:],
            "toasts": list(self.toasts),
        }

    # ===================================================================== actions
    def dispatch(self, name: str, args=None):
        args = args or {}
        fn = getattr(self, "act_" + name, None)
        if fn is None:
            raise ValueError(f"unknown action {name}")
        try:
            result = fn(**args)
        except (ValueError, FileNotFoundError, OSError, CalibrationError) as e:
            self.toast(str(e), "error")
            result = {"ok": False, "error": str(e)}
        self.on_change()
        return result

    # ---- settings
    def act_set(self, key: str, value):
        if key not in C.DEFAULTS:
            raise ValueError(f"unknown setting {key}")
        self.cfg[key] = value
        if key == "preset" and value in PRESETS and value != "custom":
            _, w, h, r, fps, h265 = PRESETS[value]
            self.cfg.update(width=w, height=h, refresh=r, max_fps=fps, h265=h265)
        if key == "always_on_top" and IS_WIN and self.mirror_hwnd:
            mirrorwin.set_topmost(self.mirror_hwnd, bool(value))
        if key == "control_mode" and self.engine:
            self.engine.set_mode(value)
            if value == "tap" and not self.ctl.calibration.valid:
                self.toast("Для режима «клик = тап» выполните калибровку", "info")
        if key in ("sensitivity", "wheel_multiplier", "swap_ctrl_cmd", "invert_wheel", "release_hotkey") and self.engine:
            e = self.engine
            e.sensitivity = float(self.cfg["sensitivity"])
            e.wheel_mult = float(self.cfg["wheel_multiplier"])
            e.swap_ctrl_cmd = bool(self.cfg["swap_ctrl_cmd"])
            e.invert_wheel = bool(self.cfg["invert_wheel"])
            e.hotkey_vk = HOTKEYS.get(self.cfg["release_hotkey"], HOTKEYS["rctrl"])[0]
        if key == "forced_ip":
            self.advertiser.forced_ip = value or ""
            self.advertiser.restart()
        self.save()
        return {"ok": True}

    # ---- receiver
    def act_start(self):
        if self.receiver.running:
            return {"ok": True}
        self.receiver.start(self.cfg)
        return {"ok": True}

    def act_stop(self):
        self.receiver.stop()
        self.advertiser.stop()
        return {"ok": True}

    def act_fit(self):
        info = self.receiver.info
        if IS_WIN and self.mirror_hwnd and info.width:
            mirrorwin.fit_to_screen(self.mirror_hwnd, info.width, info.height)
        return {"ok": True}

    def act_fullscreen(self):
        if IS_WIN and self.mirror_hwnd:
            self.fullscreen.toggle(self.mirror_hwnd)
        return {"ok": True}

    def act_reannounce(self):
        self.advertiser.restart()
        return {"ok": True}

    def act_adapters(self):
        return [a.__dict__ for a in list_adapters()]

    # ---- control transport
    def connect_hid(self, transport: str, port: str = "", quiet=False):
        if self.engine:
            self.engine.release_capture()
        if self.hid:
            self.hid.stop()
        self.hid = None
        self.hid_error = ""
        self.cfg["transport"] = transport
        if transport == "ble":
            if not winble.available():
                self.hid_error = "Bluetooth-модули WinRT недоступны в этой сборке"
                return
            self.hid = winble.WinBleLink(lambda k, v: self._event("hid", k, v))
            self.hid.start()
        elif transport == "esp32" and port:
            self.cfg["serial_port"] = port
            self.hid = SerialLink(lambda k, v: self._event("hid", k, v))
            self.hid.start(port)
        self.save()
        self._hid_changed()

    def act_transport(self, transport: str, port: str = ""):
        self.connect_hid(transport, port or self.cfg.get("serial_port", ""))
        return {"ok": True}

    def act_ports(self):
        return [{"dev": d, "label": l, "likely": k} for d, l, k in SerialLink.list_ports()]

    def act_flash(self, chip: str, port: str):
        if self.flashing:
            return {"ok": False}
        if not port:
            raise ValueError("Выберите COM-порт платы")
        self.cfg["flash_chip"] = chip
        if self.hid and self.cfg["transport"] == "esp32":
            self.hid.stop()
        self.flashing = True

        def work():
            ok = flasher.flash(chip, port, lambda s: self.logln(s))
            self.flashing = False
            if ok:
                self.toast("Прошивка записана, подключаюсь к плате", "ok")
                time.sleep(1.5)
                self.connect_hid("esp32", port)
            else:
                self.toast("Не удалось прошить плату — см. журнал", "error")
            self.on_change()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def act_unpair(self):
        if self.cfg["transport"] == "esp32" and self.hid:
            self.hid.write(P.unpair())
        self.toast("На iPhone: Настройки → Bluetooth → (i) → «Забыть это устройство»", "info")
        return {"ok": True}

    # ---- control actions
    def _require_phone(self):
        if not (self.hid and self.hid.connected and self.hid.phone_connected):
            raise ValueError("iPhone не подключён к iMirror по Bluetooth")

    def act_action(self, name: str):
        self._require_phone()
        if name not in ACTIONS:
            raise ValueError(name)
        bit = BUTTON_BITS.get(self.cfg["action_buttons"].get(name, "middle"), P.BTN_MIDDLE)
        self.ctl.click_buttons(bit)
        return {"ok": True}

    def act_media(self, name: str):
        self._require_phone()
        usage = {"vol_up": P.CONSUMER_VOL_UP, "vol_down": P.CONSUMER_VOL_DOWN,
                 "play": P.CONSUMER_PLAY_PAUSE, "mute": P.CONSUMER_MUTE}[name]
        self.ctl.consumer_tap(usage)
        return {"ok": True}

    def act_spotlight(self):
        self._require_phone()
        self.ctl.key_combo(keymap.MOD_LGUI, [0x2C])
        return {"ok": True}

    def act_type(self, text: str = "", clipboard: bool = False):
        self._require_phone()
        if clipboard:
            text = read_clipboard()
        if not text:
            raise ValueError("Нет текста для печати")
        strokes, layouts, skipped = keymap.text_to_strokes(text)
        if len(layouts) > 1:
            self.toast("В тексте и латиница, и кириллица: iPhone печатает в одной раскладке", "info")
        self.ctl.type_text(strokes)
        msg = f"Печатаю {len(strokes)} символов на iPhone"
        if skipped:
            msg += f" (пропущено {len(skipped)})"
        self.toast(msg, "ok")
        return {"ok": True}

    def act_capture(self):
        if self.engine and self.engine.enabled:
            self.engine.start_capture()
        return {"ok": True}

    # ---- calibration
    def act_calibrate(self):
        if not IS_WIN:
            raise ValueError("Калибровка доступна только в Windows")
        self._require_phone()
        hwnd, w, h = self.mirror
        if not hwnd or not w:
            raise ValueError("Сначала запустите трансляцию экрана iPhone")
        self.engine.release_capture()
        self.calib = CalibrationSession(w, h)
        self.calib_report = ""
        self._calib_next()
        return {"ok": True}

    def act_calibrate_cancel(self):
        self.calib = None
        if self.engine:
            self.engine.calib_click = None
        return {"ok": True}

    def _calib_next(self):
        cur = self.calib.current()
        self.calib_step = len(self.calib.results) + 1
        self.ctl.calib_probe(*cur)
        self.engine.calib_click = True

    def _calib_click(self, x, y):
        if not self.calib:
            return
        hwnd, w, h = self.mirror
        vr = mirrorwin.video_rect(hwnd, w, h) if hwnd else None
        if not vr:
            self.act_calibrate_cancel()
            return
        sx, sy = screen_to_stream(x, y, vr, w, h)
        try:
            self.calib.add_click(sx, sy)
        except CalibrationError as e:
            self.toast(str(e), "error")
            self._calib_next()
            return
        if not self.calib.finished():
            self._calib_next()
            return
        cal, report = self.calib.result()
        self.ctl.calibration = cal
        self.ctl.forget_position()
        self.cfg["calibration"] = cal.to_dict()
        self.save()
        self.calib = None
        self.calib_report = report
        self.ctl.point_at(w / 2, h / 2)
        self.toast("Калибровка завершена — указатель в центре экрана", "ok")

    # ===================================================================== events
    def _event_mdns(self, kind, val):
        self.mdns = {"ok": val.get("ok"), "addresses": val.get("addresses", []), "error": val.get("error", "")}
        if val.get("ok"):
            self.logln("Объявлено в сети: " + ", ".join(val["addresses"]))
        else:
            self.toast("Не удалось объявить приёмник в сети: " + val.get("error", ""), "error")
        self.on_change()

    def _event(self, src, kind, val):
        if src == "rx":
            if kind == "log":
                self.logln(val)
            elif kind == "stats":
                self.fps_hist.append(val.fps)
            elif kind == "video":
                self.ctl.src = (val.width, val.height)
                self.ctl.forget_position()
                self.logln(f"Видео от iPhone: {val.codec.upper()} {val.width}×{val.height}")
                if self.cfg["auto_fit"]:
                    threading.Timer(0.7, self.act_fit).start()
            elif kind == "conn":
                if val.connections > 0:
                    self.toast("iPhone подключился", "ok")
                else:
                    self.fps_hist.extend([0.0] * 60)
            elif kind == "state":
                if str(val).startswith("stopped"):
                    code = str(val).split(":", 1)[1]
                    self.advertiser.stop()
                    self.mdns = {"ok": None, "addresses": [], "error": ""}
                    if code not in ("0", "1", "-15", "15"):
                        self.toast(f"Приёмник завершился с кодом {code} — см. журнал", "error")
                    if self.engine:
                        self.engine.release_capture()
                elif val == "running":
                    self.logln("Приёмник запущен")
        elif src == "hid":
            if kind == "log":
                self.logln(val)
            elif kind == "esp":
                if val == "ready":
                    self.hid_error = ""
                    self.logln(f"Управление готово: {self.hid.fw_info if self.hid else ''}")
                    if self.cfg["transport"] == "esp32" and self.hid:
                        try:
                            self.hid.write(P.interval(int(self.cfg.get("report_interval_ms", 8))))
                        except (TypeError, ValueError):
                            pass
                elif val == "closed":
                    self.logln("Устройство управления отключено")
                elif str(val).startswith("error:"):
                    self.hid_error = str(val)[6:]
                self._hid_changed()
            elif kind == "phone":
                if val:
                    self.toast("iPhone подключён по Bluetooth — управление активно", "ok")
                self._hid_changed()
        elif src == "in":
            if kind == "capture":
                self.captured = bool(val)
            elif kind == "calib_click":
                self._calib_click(*val)
            elif kind == "need_calibration":
                self.toast("Сначала выполните калибровку", "info")
            elif kind == "log":
                self.logln(val)
        self.on_change()

    def _hid_changed(self):
        ok = bool(self.hid and self.hid.connected and self.hid.phone_connected)
        if self.engine:
            self.engine.enabled = ok
            if not ok:
                self.engine.release_capture()

    def _tracker(self):
        while not self._stop.wait(0.4):
            try:
                if not IS_WIN:
                    continue
                pid = self.receiver.pid
                hwnd = self.mirror_hwnd
                if not pid:
                    hwnd = None
                elif not hwnd or not win32.IsWindow(hwnd) or not win32.IsWindowVisible(hwnd):
                    new = mirrorwin.find_window_for_pid(pid)
                    if new and new != hwnd:
                        mirrorwin.set_topmost(new, self.cfg["always_on_top"])
                    hwnd = new
                changed = hwnd != self.mirror_hwnd
                self.mirror_hwnd = hwnd
                info = self.receiver.info
                self.mirror = (hwnd, info.width, info.height) if hwnd else (None, 0, 0)
                if not hwnd and self.engine and self.engine.captured:
                    self.engine.release_capture()
                if changed:
                    self.on_change()
            except Exception as e:
                self.log.append(f"tracker: {e!r}")

    def shutdown(self):
        self._stop.set()
        if self.engine:
            self.engine.stop()
        self.receiver.stop()
        self.advertiser.stop()
        if self.hid:
            self.hid.stop()
        self.save()


class _LinkProxy:
    """Controller writes here; forwards to whichever HID transport is active."""

    def __init__(self, core):
        self.core = core

    def write(self, data: bytes) -> bool:
        link = self.core.hid
        return bool(link and link.write(data))

    def on_event(self, kind, val):
        self.core._event("hid", kind, val)


def read_clipboard() -> str:
    if not IS_WIN:
        return ""
    CF_UNICODETEXT = 13
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.GetClipboardData.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u32.OpenClipboard(None):
        return ""
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else ""
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()
