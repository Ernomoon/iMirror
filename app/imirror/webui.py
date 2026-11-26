"""Window host: Edge WebView2 (pywebview) showing ui/index.html, bridged to Core.

JS -> Python: window.pywebview.api.call(name, args) / .state() / window controls.
Python -> JS: window.__imirror.push(state) at most ~12 times per second.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"


class Api:
    """Exposed to JavaScript. Keep attributes private (_x): pywebview exposes public ones."""

    def __init__(self, host):
        self._host = host

    def state(self):
        return self._host.core.state()

    def call(self, name, args=None):
        try:
            return self._host.core.dispatch(name, args or {})
        except Exception as e:      # never leave a JS promise hanging
            return {"ok": False, "error": str(e)}

    def ready(self):
        self._host.ui_ready.set()
        return True

    def minimize(self):
        self._host.window.minimize()

    def toggle_maximize(self):
        w = self._host.window
        if self._host.maximized:
            w.restore()
        else:
            w.maximize()
        self._host.maximized = not self._host.maximized

    def close(self):
        self._host.window.destroy()

    def open_url(self, url):
        import webbrowser
        if str(url).startswith("https://"):
            webbrowser.open(url)


class Host:
    def __init__(self, selftest=False):
        from .core import Core
        self.selftest = selftest
        self.ui_ready = threading.Event()
        self.maximized = False
        self._dirty = threading.Event()
        self.core = Core(on_change=self._dirty.set)
        self.window = None
        self.exit_code = 0

    def _pusher(self):
        self.ui_ready.wait()
        last = 0.0
        while True:
            self._dirty.wait(1.0)
            self._dirty.clear()
            dt = time.monotonic() - last
            if dt < 0.08:
                time.sleep(0.08 - dt)
            last = time.monotonic()
            try:
                payload = json.dumps(self.core.state(), ensure_ascii=False)
                self.window.evaluate_js(f"window.__imirror && window.__imirror.push({payload})")
            except Exception:
                if self.window is None:
                    return

    def _selftest(self):
        """CI check of the frozen app: WebView2 + JS bridge + core all work."""
        ok = self.ui_ready.wait(90)
        result = {"ui_ready": ok}
        try:
            if ok:
                result["js"] = self.window.evaluate_js("JSON.stringify({pages: document.querySelectorAll('[data-page]').length, title: document.title, rendered: !!window.__imirror && window.__imirror.rendered})")
                st = self.core.state()
                result["transport"] = st["hid"]["transport"]
                result["ble_available"] = st["hid"]["ble_available"]
                time.sleep(4)          # let BLE init report its status
                result["hid_error"] = self.core.state()["hid"]["error"]
                result["hid_ready"] = self.core.state()["hid"]["ready"]
                shot = Path.cwd() / "selftest-ui.png"
                try:
                    from PIL import ImageGrab
                    x, y, w, h = self.window.x, self.window.y, self.window.width, self.window.height
                    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(shot)
                    result["screenshot"] = str(shot)
                except Exception as e:
                    result["screenshot_error"] = repr(e)
        except Exception as e:
            result["error"] = repr(e)
            ok = False
        Path("selftest-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        self.exit_code = 0 if ok and "error" not in result else 1
        self.window.destroy()

    def run(self):
        import webview
        self.window = webview.create_window(
            "iMirror", url=str(UI_DIR / "index.html"), js_api=Api(self),
            width=1180, height=780, min_size=(980, 660), frameless=True, easy_drag=False,
            background_color="#07070c", text_select=False)
        threading.Thread(target=self._pusher, daemon=True).start()
        if self.selftest:
            threading.Thread(target=self._selftest, daemon=True).start()
        try:
            webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug="--debug" in sys.argv)
        finally:
            self.core.shutdown()
        return self.exit_code


def main():
    if sys.platform == "win32":
        from . import win32
        win32.enable_dpi_awareness()
    sys.setswitchinterval(0.001)
    host = Host(selftest="--selftest" in sys.argv)
    sys.exit(host.run())
