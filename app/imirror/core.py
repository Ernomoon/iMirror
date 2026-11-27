"""iMirror application core: AirPlay receiver, network announcement, self-check.

The UI (webui.py + ui/*) only reads `Core.state()` snapshots and calls `Core.dispatch()`.
"""
from __future__ import annotations

import collections
import logging
import sys
import threading
import time
from pathlib import Path

from . import config as C
from .mdns import Advertiser, list_adapters
from .receiver import Receiver, PRESETS, VIDEO_SINKS, find_uxplay
from . import netdiag

IS_WIN = sys.platform == "win32"
if IS_WIN:
    from . import win32, mirrorwin
else:
    win32 = mirrorwin = None

log = logging.getLogger("imirror")


METHOD_TITLES = {"native": "встроенной службой Windows", "bonjour": "через Bonjour",
                 "zeroconf": "собственным объявлением"}


class Core:
    def __init__(self, on_change=None):
        self.cfg = C.load()
        self.on_change = on_change or (lambda: None)
        self.log = collections.deque(maxlen=400)
        self.toasts = collections.deque(maxlen=20)
        self._toast_id = 0

        self.advertiser = Advertiser(self._event_mdns)
        fip = self.cfg.get("forced_ip", "")
        if fip and not any(a.ip == fip and not a.virtual for a in list_adapters()):
            log.info("dropping forced adapter %s (virtual or gone)", fip)
            fip = self.cfg["forced_ip"] = ""
        self.advertiser.forced_ip = fip
        self.mdns = {"ok": None, "addresses": [], "error": "", "method": ""}
        self.receiver = Receiver(lambda k, v: self._event(k, v), line_hook=self.advertiser.feed)
        self.wifi = netdiag.WifiStreaming()
        self.control = None
        if IS_WIN:
            try:
                from .control import Control
                self.control = Control(self.on_change, self.logln, lambda: self.mirror_hwnd)
                if self.cfg.get("control_enabled"):
                    threading.Timer(1.5, lambda: self.dispatch("control", {"op": "on"})).start()
            except Exception as e:
                log.warning("control unavailable: %r", e)
        self.checks: list = []
        self.checking = False
        self._last_check = 0.0

        self.mirror_hwnd = None
        self.fullscreen = mirrorwin.FullscreenToggler() if IS_WIN else None
        self.fps_hist = collections.deque([0.0] * 60, maxlen=60)
        self.connected_since = 0.0
        self.fault: dict | None = None          # why the receiver is not usable right now
        self._safe_mode = False
        self._start_gen = 0
        self._stop = threading.Event()
        threading.Thread(target=self._tracker, name="iMirror-tracker", daemon=True).start()
        self.run_checks()
        if self.cfg.get("autostart", True) and find_uxplay(self.cfg):
            threading.Timer(0.8, lambda: self.dispatch("start")).start()

    # ===================================================================== helpers
    def logln(self, text: str):
        self.log.append(time.strftime("%H:%M:%S ") + text)
        log.info(text)
        self.on_change()

    def toast(self, text: str, kind: str = "info"):
        self._toast_id += 1
        self.toasts.append({"id": self._toast_id, "text": text, "kind": kind})
        self.logln(text)

    def save(self):
        try:
            C.save(self.cfg)
        except OSError as e:
            log.warning("save settings: %s", e)

    def _paths(self):
        app = sys.executable if getattr(sys, "frozen", False) else str(Path(sys.executable))
        ux = find_uxplay(self.cfg) or ""
        return app, ux

    # ===================================================================== state
    def phase(self) -> str:
        r = self.receiver
        if not r.running:
            return "problem" if self.fault else "off"
        if r.info.connections > 0 and r.info.width:
            return "live"
        if r.info.connections > 0:
            return "connecting"
        if self.mdns.get("ok") is False:
            return "problem"
        if self.mdns.get("ok"):
            return "ready"
        return "starting"

    def state(self) -> dict:
        info = self.receiver.info
        return {
            "platform": sys.platform,
            "phase": self.phase(),
            "cfg": dict(self.cfg),
            "presets": {k: {"title": v[0], "w": v[1], "h": v[2], "hz": v[3], "fps": v[4], "h265": v[5]}
                        for k, v in PRESETS.items()},
            "sinks": VIDEO_SINKS,
            "receiver": {
                "running": self.receiver.running,
                "connections": info.connections,
                "codec": info.codec, "width": info.width, "height": info.height,
                "fps": round(info.fps, 1), "peak": round(info.peak_fps, 1),
                "mbps": round(info.kbps / 1000.0, 1),
                "fps_hist": list(self.fps_hist),
                "window": bool(self.mirror_hwnd),
                "since": self.connected_since,
            },
            "mdns": self.mdns,
            "fault": self.fault,
            "safe_mode": self._safe_mode,
            "control": self.control.state() if self.control else None,
            "checks": self.checks,
            "checking": self.checking,
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
        except (ValueError, FileNotFoundError, OSError) as e:
            self.toast(str(e), "error")
            result = {"ok": False, "error": str(e)}
        self.on_change()
        return result

    def act_set(self, key: str, value):
        if key not in C.DEFAULTS:
            raise ValueError(f"unknown setting {key}")
        self.cfg[key] = value
        if key == "preset" and value in PRESETS and value != "custom":
            _, w, h, r, fps, h265 = PRESETS[value]
            self.cfg.update(width=w, height=h, refresh=r, max_fps=fps, h265=h265)
        if key == "always_on_top" and IS_WIN and self.mirror_hwnd:
            mirrorwin.set_topmost(self.mirror_hwnd, bool(value))
        if key == "forced_ip":
            self.advertiser.forced_ip = value or ""
            self.advertiser.restart()
        self.save()
        return {"ok": True}

    STARTUP_LIMIT = 15.0       # seconds until the receiver must be announced

    def act_start(self, safe: bool = False):
        if self.receiver.running:
            return {"ok": True}
        self.fault = None
        self._safe_mode = bool(safe)
        self.mdns = {"ok": None, "addresses": [], "error": "", "method": ""}
        cfg = dict(self.cfg)
        if safe:       # the most compatible start: UxPlay defaults, no audio, generic sink
            cfg.update(audio=False, video_sink="autovideosink", h265=False, _minimal=True)
            self.logln("Безопасный режим: настройки UxPlay по умолчанию, без звука")
        self.receiver.start(cfg)
        if cfg.get("device_id") != self.cfg.get("device_id"):
            self.cfg["device_id"] = cfg["device_id"]
            self.save()
        self._start_gen += 1
        gen = self._start_gen
        threading.Timer(self.STARTUP_LIMIT, lambda: self._startup_watchdog(gen)).start()
        threading.Timer(6.0, self.run_checks).start()
        return {"ok": True}

    def act_safe_start(self):
        self.act_stop()
        time.sleep(0.5)
        return self.act_start(safe=True)

    def _tail(self, n=12):
        return [l for l in list(self.receiver.tail)[-n:] if l.strip()]

    def _startup_watchdog(self, gen):
        """Never stay in "starting" forever: explain what is stuck."""
        if gen != self._start_gen or not self.receiver.running or self.mdns.get("ok") is not None:
            return
        stage = self.receiver.stage
        tail = self._tail()
        log.error("startup watchdog: stage=%r, last output:\n%s", stage, "\n".join(tail))
        if stage in ("register", "ready"):
            self.fault = {"title": "Не получилось объявить компьютер в сети",
                          "detail": "Приёмник работает, но объявление в сети не ответило за "
                                    f"{int(self.STARTUP_LIMIT)} с. Нажмите «Повторить» или проверьте сеть ниже.",
                          "tail": tail, "action": "reannounce"}
            self.mdns = {"ok": False, "addresses": [], "error": "объявление не ответило", "method": ""}
            self.advertiser.restart()
        else:
            self.receiver.stop()
            if not self._safe_mode:
                self.logln("Приёмник не запустился за 15 с, пробую безопасный режим")
                self.act_start(safe=True)
                return
            self.fault = {"title": "Приёмник AirPlay не запускается",
                          "detail": "UxPlay завис при запуске даже в безопасном режиме. "
                                    "Последние строки его вывода — ниже и в журнале.",
                          "tail": tail, "action": "start"}
        self.on_change()

    def act_dismiss(self):
        self.fault = None
        return {"ok": True}

    def act_stop(self):
        self._start_gen += 1
        self.fault = None
        self.receiver.stop()
        self.advertiser.stop()
        self.mdns = {"ok": None, "addresses": [], "error": "", "method": ""}
        return {"ok": True}

    def act_restart(self):
        """Apply quality changes: restart the receiver (iPhone reconnects by itself)."""
        if self.receiver.running:
            self.act_stop()
            time.sleep(0.6)
        return self.act_start()

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
        from dataclasses import asdict
        return [asdict(a) for a in list_adapters()]

    def act_check(self):
        self.run_checks(force=True)
        return {"ok": True}

    def act_fix(self, fix: str, arg: str = ""):
        last = getattr(self, "_fix_times", {})
        self._fix_times = last
        if time.monotonic() - last.get(fix, -1e9) < 20:
            return {"ok": True, "pending": True}       # a UAC prompt for this fix is already open
        last[fix] = time.monotonic()
        if fix == "reannounce":
            self.advertiser.restart()
            return {"ok": True}
        if fix == "start":
            return self.act_start()
        app, ux = self._paths()
        if not netdiag.apply_fix(fix, arg, app, ux):
            raise ValueError("Windows не дала запустить исправление (нужно подтвердить запрос администратора)")
        self.toast("Подтвердите запрос Windows. Через несколько секунд проверю снова", "info")
        if fix == "reannounce":
            self.advertiser.restart()
        threading.Timer(6.0, lambda: self.run_checks(force=True)).start()
        threading.Timer(15.0, lambda: self.run_checks(force=True)).start()
        if fix in ("bonjour_install", "bonjour_start"):
            threading.Timer(40.0, self.advertiser.restart).start()
            threading.Timer(45.0, lambda: self.run_checks(force=True)).start()
        return {"ok": True}

    # ===================================================================== checks
    def run_checks(self, force=False):
        if self.checking or (not force and time.monotonic() - self._last_check < 5):
            return

        def work():
            self.checking = True
            self.on_change()
            try:
                app, ux = self._paths()
                self.checks = netdiag.run_checks(app, ux, self.receiver.running, self.advertiser.port, self.mdns)
                bad = [c["title"] for c in self.checks if c["state"] == "bad"]
                log.info("checks: %s", [(c["id"], c["state"]) for c in self.checks])
                if bad:
                    self.logln("Проверка нашла проблемы: " + ", ".join(bad))
            except Exception as e:
                log.exception("checks failed")
                self.checks = [{"id": "err", "title": "Проверка", "state": "warn", "detail": str(e)}]
            finally:
                self._last_check = time.monotonic()
                self.checking = False
                self.on_change()
        threading.Thread(target=work, name="iMirror-checks", daemon=True).start()

    # ===================================================================== events
    def _event_mdns(self, kind, val):
        self.mdns = {"ok": val.get("ok"), "addresses": val.get("addresses", []),
                     "error": val.get("error", ""), "method": val.get("method", "")}
        if val.get("ok"):
            self.fault = None
            how = METHOD_TITLES.get(val.get("method"), "собственным объявлением")
            self.logln(f"Объявлено в сети {how}: " + ", ".join(val.get("addresses", [])))
        else:
            self.toast("Не удалось объявить приёмник в сети: " + val.get("error", ""), "error")
        self.run_checks(force=True)
        self.on_change()

    def _event(self, kind, val):
        if kind == "log":
            self.logln(val)
        elif kind == "stage":
            log.info("receiver stage: %s", val)
            if val == "register_failed":
                self.fault = {"title": "Приёмник не смог зарегистрироваться",
                              "detail": "UxPlay сообщил об ошибке регистрации службы AirPlay.",
                              "tail": self._tail(), "action": "start"}
        elif kind == "stats":
            self.fps_hist.append(val.fps)
        elif kind == "video":
            self.logln(f"Видео от iPhone: {val.codec.upper()} {val.width}×{val.height}")
            if self.cfg["auto_fit"]:
                threading.Timer(0.7, self.act_fit).start()
        elif kind == "conn":
            if val.connections > 0:
                self.connected_since = time.time()
                self.toast("iPhone подключился", "ok")
                if self.cfg.get("wifi_boost", True):
                    self.wifi.enable(True)
            else:
                self.connected_since = 0.0
                self.fps_hist.extend([0.0] * 60)
                self.wifi.enable(False)
        elif kind == "state":
            if str(val).startswith("stopped"):
                code = str(val).split(":", 1)[1]
                self.advertiser.stop()
                self.mdns = {"ok": None, "addresses": [], "error": "", "method": ""}
                self.wifi.enable(False)
                tail = self._tail()
                log.info("receiver exited with code %s (requested=%s); last output:\n%s",
                         code, self.receiver.stopping, "\n".join(tail))
                if not self.receiver.stopping and not self._safe_mode and self.receiver.stage == "":
                    self.logln(f"Приёмник упал при запуске (код {code}), пробую безопасный режим")
                    threading.Timer(1.0, lambda: self.act_start(safe=True)).start()
                elif not self.receiver.stopping:
                    hint = _exit_hint(code, tail)
                    self.fault = {"title": "Приёмник AirPlay остановился",
                                  "detail": f"{hint} (код {code}).", "tail": tail, "action": "start"}
                    self.toast(f"Приёмник остановился: {hint}", "error")
            elif val == "running":
                self.logln("Приёмник запущен")
        self.on_change()

    def act_control(self, op: str, **kw):
        if not self.control:
            raise ValueError("Управление недоступно на этом компьютере")
        res = self.control.dispatch(op, **kw)
        if op in ("on", "off"):
            self.cfg["control_enabled"] = op == "on"
            self.save()
        if isinstance(res, dict) and res.get("note"):
            self.toast(res["note"], "info")
        return res

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
                if changed:
                    self.on_change()
            except Exception as e:
                log.warning("tracker: %r", e)

    def shutdown(self):
        self._stop.set()
        self.receiver.stop()
        self.advertiser.stop()
        self.wifi.enable(False)
        if self.control:
            self.control.shutdown()
        self.save()


def _exit_hint(code: str, tail: list) -> str:
    text = "\n".join(tail).lower()
    if "address already in use" in text or "10048" in text or "bind" in text and "fail" in text:
        return "порт занят другой программой AirPlay (AirServer, LonelyScreen?) — закройте её"
    if "no element" in text or "could not" in text and "sink" in text:
        return "не хватает модуля видео, попробуйте безопасный режим"
    if code in ("3221225501", "-1073741795"):
        return "приёмник упал (недопустимая инструкция процессора)"
    if code in ("3221225477", "-1073741819"):
        return "сбой приёмника (ошибка доступа к памяти)"
    if code in ("3221225781", "-1073741515"):
        return "не найдена библиотека приёмника — переустановите iMirror"
    return "приёмник неожиданно завершился"
