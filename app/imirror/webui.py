"""Window host: Edge WebView2 (pywebview) showing ui/index.html, bridged to Core.

JS -> Python: window.pywebview.api.call(name, args) / .state() / window controls.
Python -> JS: window.__imirror.push(state) at most ~12 times per second.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("imirror")


def _setup_logging():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "iMirror"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "imirror.log"
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(base / "imirror.old.log")
    except OSError:
        pass
    logging.basicConfig(filename=str(path), level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")

    def hook(exc_type, exc, tb):
        log.error("uncaught", exc_info=(exc_type, exc, tb))
    sys.excepthook = hook
    threading.excepthook = lambda a: log.error("thread %s crashed", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    return path


def _window_size(want_w=1180, want_h=780):
    """Fit the window into the monitor's work area (logical pixels)."""
    if sys.platform != "win32":
        return want_w, want_h
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
        dpi = ctypes.windll.user32.GetDpiForSystem() if hasattr(ctypes.windll.user32, "GetDpiForSystem") else 96
        scale = max(dpi, 96) / 96.0
        aw, ah = (r.right - r.left) / scale, (r.bottom - r.top) / scale
        return int(min(want_w, aw - 24)), int(min(want_h, ah - 24))
    except Exception:
        return want_w, want_h

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
        log.info("selftest result %s", json.dumps(result, ensure_ascii=False))
        self.exit_code = 0 if ok and "error" not in result else 1
        log.info("selftest done %s, closing window", self.exit_code)
        self.window.destroy()
        log.info("window.destroy() returned")

    def run(self):
        import webview
        w, h = _window_size()
        log.info("window %dx%d", w, h)
        self.window = webview.create_window(
            "iMirror", url=str(UI_DIR / "index.html"), js_api=Api(self),
            width=w, height=h, min_size=(min(w, 900), min(h, 600)), frameless=True, easy_drag=False,
            background_color="#07070c", text_select=False)
        threading.Thread(target=self._pusher, daemon=True).start()
        if self.selftest:
            threading.Thread(target=self._selftest, daemon=True).start()
        try:
            webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug="--debug" in sys.argv)
        except Exception:
            log.exception("webview failed")
            self.exit_code = self.exit_code or 2
        finally:
            log.info("window closed, shutting down")
            done = threading.Event()

            def _shutdown():
                try:
                    self.core.shutdown()
                finally:
                    done.set()
            threading.Thread(target=_shutdown, daemon=True).start()
            done.wait(6)
        return self.exit_code


def _safe_stdio():
    """Windowed .exe: stdout/stderr may be None or a cp1252 pipe; never let output crash a thread."""
    import io
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, io.StringIO())
        else:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass


def main():
    _safe_stdio()
    path = _setup_logging()
    log.info("iMirror start %s, log %s", sys.argv, path)
    if sys.platform == "win32":
        from . import win32
        win32.enable_dpi_awareness()
    sys.setswitchinterval(0.001)
    if "--selftest" in sys.argv:
        import faulthandler
        stacks = open(path.parent / "stacks.txt", "w", encoding="utf-8")
        faulthandler.dump_traceback_later(100, repeat=False, file=stacks)
    host = Host(selftest="--selftest" in sys.argv)
    code = host.run()
    log.info("exit %s", code)
    logging.shutdown()
    # The .NET runtime behind WebView2 (pythonnet) can hang interpreter shutdown: exit hard
    # after everything of ours has been stopped and saved.
    os._exit(code)
