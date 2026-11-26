"""iPhone control from the PC over Bluetooth: state + actions for the UI."""
from __future__ import annotations

import logging
import threading
import time

from . import blehid, keymap

log = logging.getLogger("imirror")


class Control:
    def __init__(self, on_change, logln, get_mirror_hwnd):
        self.on_change = on_change
        self.logln = logln
        self.get_mirror = get_mirror_hwnd
        self.available = blehid.available()
        self.hid = blehid.BleHid(on_change)
        self.capture = None
        self.captured = False
        self.typing = False

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        h = self.hid
        return {"available": self.available, "status": h.status, "error": h.error,
                "adapter": h.adapter, "captured": self.captured, "typing": self.typing}

    def _ensure_capture(self):
        if self.capture is None:
            from .capture import InputCapture
            self.capture = InputCapture(self.hid, self.get_mirror, self._capture_event)

    def _capture_event(self, kind, val):
        if kind == "capture":
            self.captured = bool(val)
            self.on_change()
        elif kind == "log":
            self.logln(str(val))

    # ------------------------------------------------------------------ actions
    def dispatch(self, op: str, **kw):
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            raise ValueError(f"unknown control op {op}")
        res = fn(**kw)
        self.on_change()
        return res or {"ok": True}

    def op_on(self):
        if not self.available:
            raise ValueError("Не хватает компонентов Bluetooth Windows (WinRT). Переустановите iMirror")
        self.hid.start()
        self._ensure_capture()
        self.logln("Bluetooth-управление включено: подключите iPhone в Настройки → Bluetooth")

    def op_off(self):
        if self.capture:
            self.capture.release_capture()
        self.hid.stop()

    def op_capture(self):
        if not self.hid.connected:
            raise ValueError("Сначала подключите iPhone к компьютеру по Bluetooth")
        self._ensure_capture()
        self.capture.start_capture()

    def op_release(self):
        if self.capture:
            self.capture.release_capture()

    def op_home(self):
        self._need()
        self.hid.nav_tap(blehid.NAV_MENU)

    def op_switcher(self):
        self._need()
        self.hid.nav_tap(blehid.NAV_MENU, double=True)

    def op_search(self):
        self._need()
        self.hid.nav_tap(blehid.NAV_SEARCH)

    def op_back(self):
        self._need()
        self.hid.nav_tap(blehid.NAV_BACK)

    def op_media(self, key: str):
        self._need()
        usage = blehid.CONSUMER.get(key)
        if usage is None:
            raise ValueError(key)
        self.hid.consumer_tap(usage)

    def op_type(self, text: str):
        self._need()
        if self.typing:
            return {"ok": False}
        strokes, layouts, skipped = keymap.text_to_strokes(text[:4000])

        def run():
            self.typing = True
            self.on_change()
            try:
                for mods, usage in strokes:
                    self.hid.keys(mods, [usage])
                    self.hid.keys(0, [])
                    time.sleep(0.012)
            finally:
                self.typing = False
                self.on_change()
        threading.Thread(target=run, name="iMirror-type", daemon=True).start()
        note = ""
        if "ru" in layouts and "us" in layouts:
            note = "Текст смешанный: на iPhone раскладка переключается вручную (Globe)"
        elif "ru" in layouts:
            note = "Включите на iPhone русскую раскладку аппаратной клавиатуры"
        if skipped:
            note += (". " if note else "") + f"Пропущено символов: {len(skipped)}"
        return {"ok": True, "note": note}

    def _need(self):
        if not self.hid.connected:
            raise ValueError("iPhone не подключён по Bluetooth")

    def shutdown(self):
        try:
            if self.capture:
                self.capture.stop()
            self.hid.stop()
        except Exception as e:
            log.warning("control shutdown: %r", e)
