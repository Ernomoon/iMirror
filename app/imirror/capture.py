"""Windows input capture: forwards the PC mouse/keyboard to the iPhone.

Modes
  off      – nothing is intercepted.
  capture  – "like a virtual machine": click inside the mirror window (or press the release
             hotkey) and the whole mouse + keyboard go to the iPhone; the iPhone's pointer
             moves 1:1 with raw mouse counts. Press the hotkey (Right Ctrl by default) to get
             the PC back.
  tap      – "click = tap": clicks on the mirror image become taps at that exact spot
             (needs calibration); dragging = swipe, right click = long press, wheel = scroll;
             keyboard goes to the iPhone while the mirror has focus (last click was on it).

Technique: WH_MOUSE_LL / WH_KEYBOARD_LL hooks decide what is swallowed; mouse movement
comes from Raw Input (true sensor counts, no Windows acceleration, never stuck at a screen
edge). If Raw Input is unavailable, movement is computed from hook coordinates around a
parked cursor.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as W

from . import win32 as U
from . import keymap
from . import protocol as P
from . import mirrorwin
from .calib import screen_to_stream

HOTKEYS = {
    "rctrl": (0xA3, "Правый Ctrl"),
    "ralt": (0xA5, "Правый Alt"),
    "scroll": (0x91, "Scroll Lock"),
    "pause": (0x13, "Pause/Break"),
}

WM_APP_STOP = U.WM_USER + 77

_BTN_DOWN = {U.WM_LBUTTONDOWN: P.BTN_LEFT, U.WM_RBUTTONDOWN: P.BTN_RIGHT, U.WM_MBUTTONDOWN: P.BTN_MIDDLE}
_BTN_UP = {U.WM_LBUTTONUP: P.BTN_LEFT, U.WM_RBUTTONUP: P.BTN_RIGHT, U.WM_MBUTTONUP: P.BTN_MIDDLE}


def _hiword_signed(v: int) -> int:
    return ctypes.c_short((v >> 16) & 0xFFFF).value


class InputEngine:
    def __init__(self, controller, get_mirror, on_event, cfg):
        """
        controller : bridge.Controller
        get_mirror : callable -> (hwnd, src_w, src_h) or (None, 0, 0)
        on_event   : callable(kind, payload), called from the hook thread (must be quick)
        """
        self.ctl = controller
        self.get_mirror = get_mirror
        self.on_event = on_event
        self.mode = cfg.get("control_mode", "capture")
        self.sensitivity = float(cfg.get("sensitivity", 1.0))
        self.wheel_mult = float(cfg.get("wheel_multiplier", 1.0))
        self.invert_wheel = bool(cfg.get("invert_wheel", False))
        self.swap_ctrl_cmd = bool(cfg.get("swap_ctrl_cmd", True))
        self.hotkey_vk = HOTKEYS.get(cfg.get("release_hotkey", "rctrl"), HOTKEYS["rctrl"])[0]
        self.enabled = True                 # master switch (e.g. ESP32 not connected -> False)

        self.captured = False
        self.tap_focus = False
        self.calib_click = None             # truthy -> next click on the mirror is a calibration click

        self._keys = keymap.KeyState()
        self._buttons = 0
        self._hotkey_down = False
        self._hotkey_clean = False          # no other key pressed while hotkey held
        self._hotkey_swallowed = False
        self._raw_seen = 0.0
        self._park = (0, 0)
        self._acc = [0.0, 0.0]
        self._wheel_acc = [0.0, 0.0]
        self._tap_btn = 0                   # mouse button held in tap mode
        self._tap_last = None               # last screen point during a tap drag
        self._swallow_up = set()            # button-up messages to swallow (pressed while ours)

        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="iMirror-input", daemon=True)
        self._thread.start()
        self._ready.wait(3)

    # ---------------------------------------------------------------- public (any thread)
    def set_mode(self, mode: str):
        if mode != self.mode:
            self.release_capture()
            self.tap_focus = False
            self.mode = mode

    def start_capture(self):
        hwnd, _, _ = self.get_mirror()
        if not self.enabled or not hwnd or self.captured:
            return False
        rect = mirrorwin.client_rect_screen(hwnd)
        if rect:
            self._park = (int(rect.x + rect.w / 2), int(rect.y + rect.h / 2))
        else:
            pt = U.POINT()
            U.GetCursorPos(ctypes.byref(pt))
            self._park = (pt.x, pt.y)
        U.SetCursorPos(*self._park)
        self._keys.clear()
        self._buttons = 0
        self._acc = [0.0, 0.0]
        self.ctl.release_all()
        self.captured = True
        self.on_event("capture", True)
        return True

    def release_capture(self):
        if not self.captured:
            return
        self.captured = False
        self._keys.clear()
        self._buttons = 0
        self.ctl.release_all()
        self.on_event("capture", False)

    def stop(self):
        self.release_capture()
        if self._thread_id:
            U.PostThreadMessageW(self._thread_id, U.WM_QUIT, 0, 0)

    # ---------------------------------------------------------------- thread
    def _run(self):
        self._thread_id = U.GetCurrentThreadId()
        hinst = U.GetModuleHandleW(None)

        self._wndproc = U.WNDPROC(self._wnd_proc)
        wc = U.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(U.WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "iMirrorRawInput"
        U.RegisterClassExW(ctypes.byref(wc))
        self._hwnd = U.CreateWindowExW(0, "iMirrorRawInput", "iMirror", 0, 0, 0, 0, 0,
                                       U.HWND_MESSAGE, None, hinst, None)
        if self._hwnd:
            rid = U.RAWINPUTDEVICE(0x01, 0x02, U.RIDEV_INPUTSINK, self._hwnd)
            if not U.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(U.RAWINPUTDEVICE)):
                self.on_event("log", "Raw Input недоступен — движение будет считаться по курсору")

        self._mouse_proc = U.HOOKPROC(self._mouse_hook)
        self._kbd_proc = U.HOOKPROC(self._kbd_hook)
        self._mh = U.SetWindowsHookExW(U.WH_MOUSE_LL, self._mouse_proc, hinst, 0)
        self._kh = U.SetWindowsHookExW(U.WH_KEYBOARD_LL, self._kbd_proc, hinst, 0)
        if not self._mh or not self._kh:
            self.on_event("log", f"Не удалось установить перехват ввода (ошибка {ctypes.get_last_error()})")
        self._ready.set()

        msg = U.MSG()
        while U.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            U.TranslateMessage(ctypes.byref(msg))
            U.DispatchMessageW(ctypes.byref(msg))

        if self._mh:
            U.UnhookWindowsHookEx(self._mh)
        if self._kh:
            U.UnhookWindowsHookEx(self._kh)
        if self._hwnd:
            U.DestroyWindow(self._hwnd)

    # ---------------------------------------------------------------- raw input (movement)
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == U.WM_INPUT:
            try:
                self._on_raw(lparam)
            except Exception as e:
                self.on_event("log", f"raw input: {e!r}")
            return U.DefWindowProcW(hwnd, msg, wparam, lparam)
        return U.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_raw(self, lparam):
        if not self.captured:
            return
        ri = U.RAWINPUT_MOUSE()
        size = W.UINT(ctypes.sizeof(ri))
        got = U.GetRawInputData(W.HANDLE(lparam), U.RID_INPUT, ctypes.byref(ri), ctypes.byref(size),
                                ctypes.sizeof(U.RAWINPUTHEADER))
        if got == 0xFFFFFFFF or got == 0 or ri.header.dwType != U.RIM_TYPEMOUSE:
            return
        m = ri.mouse
        if m.usFlags & U.MOUSE_MOVE_ABSOLUTE:
            return            # tablets / remote desktop: fall back to hook-based deltas
        self._raw_seen = time.monotonic()
        if m.lLastX or m.lLastY:
            self._add_move(m.lLastX, m.lLastY)

    def _add_move(self, dx, dy):
        self.ctl.move(dx * self.sensitivity, dy * self.sensitivity)

    # ---------------------------------------------------------------- mouse hook
    def _mouse_hook(self, ncode, wparam, lparam):
        try:
            if ncode == U.HC_ACTION and self.enabled:
                info = ctypes.cast(lparam, ctypes.POINTER(U.MSLLHOOKSTRUCT)).contents
                if not info.flags & U.LLMHF_INJECTED:
                    if self._handle_mouse(int(wparam), info):
                        return 1
        except Exception as e:
            self.on_event("log", f"mouse hook: {e!r}")
        return U.CallNextHookEx(None, ncode, wparam, lparam)

    def _handle_mouse(self, msg, info) -> bool:
        """Return True to swallow the event."""
        x, y = info.pt.x, info.pt.y

        # A pending calibration click (any mode)
        if self.calib_click is not None and msg == U.WM_LBUTTONDOWN:
            hwnd, _, _ = self.get_mirror()
            if hwnd and mirrorwin.same_hwnd(mirrorwin.window_under(x, y), hwnd):
                self.calib_click = None
                self._swallow_up.add(U.WM_LBUTTONUP)
                self.on_event("calib_click", (x, y))
                return True
        if msg in self._swallow_up:
            self._swallow_up.discard(msg)
            return True

        if self.captured:
            return self._captured_mouse(msg, info, x, y)
        if self.mode == "capture" and msg == U.WM_LBUTTONDOWN:
            hwnd, _, _ = self.get_mirror()
            if hwnd and mirrorwin.same_hwnd(mirrorwin.window_under(x, y), hwnd):
                if self.start_capture():
                    self._swallow_up.add(U.WM_LBUTTONUP)
                    return True
            return False
        if self.mode == "tap":
            return self._tap_mouse(msg, info, x, y)
        return False

    def _captured_mouse(self, msg, info, x, y) -> bool:
        if msg == U.WM_MOUSEMOVE:
            # Raw Input is the primary source; use hook deltas only if raw input is silent.
            if time.monotonic() - self._raw_seen > 0.25:
                dx, dy = x - self._park[0], y - self._park[1]
                if dx or dy:
                    self._add_move(dx, dy)
            return True
        if msg in _BTN_DOWN:
            self._buttons |= _BTN_DOWN[msg]
            self.ctl.buttons(self._buttons)
        elif msg in _BTN_UP:
            self._buttons &= ~_BTN_UP[msg]
            self.ctl.buttons(self._buttons)
        elif msg in (U.WM_XBUTTONDOWN, U.WM_XBUTTONUP):
            bit = P.BTN_BACK if (info.mouseData >> 16) & 0xFFFF == 1 else P.BTN_FORWARD
            if msg == U.WM_XBUTTONDOWN:
                self._buttons |= bit
            else:
                self._buttons &= ~bit
            self.ctl.buttons(self._buttons)
        elif msg in (U.WM_MOUSEWHEEL, U.WM_MOUSEHWHEEL):
            self._wheel(msg, info)
        return True

    def _wheel(self, msg, info, at=None):
        delta = _hiword_signed(info.mouseData) / 120.0 * self.wheel_mult
        if self.invert_wheel:
            delta = -delta
        if msg == U.WM_MOUSEWHEEL:
            w, p = delta, 0.0
        else:
            w, p = 0.0, delta
        if at is None:
            self.ctl.scroll(w, p)
        else:
            self.ctl.scroll_at(at[0], at[1], w, p)

    def _tap_mouse(self, msg, info, x, y) -> bool:
        hwnd, sw, sh = self.get_mirror()
        cal = self.ctl.calibration
        # drag in progress: follow the cursor anywhere on screen
        if self._tap_btn:
            if msg == U.WM_MOUSEMOVE:
                vr = mirrorwin.video_rect(hwnd, sw, sh) if hwnd else None
                if vr and self._tap_last:
                    dx = (x - self._tap_last[0]) / vr.w * sw
                    dy = (y - self._tap_last[1]) / vr.h * sh
                    self.ctl.drag_px(dx, dy)
                self._tap_last = (x, y)
                return False                       # let the Windows cursor move visibly
            if msg in _BTN_UP and _BTN_UP[msg] == self._tap_btn:
                self._tap_btn = 0
                self._tap_last = None
                self.ctl.buttons(0)
                return True
            return msg in _BTN_DOWN or msg in _BTN_UP

        if msg == U.WM_MOUSEMOVE or not hwnd or not sw:
            return False                           # hot path: plain moves are never touched
        inside = mirrorwin.same_hwnd(mirrorwin.window_under(x, y), hwnd)
        if msg in _BTN_DOWN or msg in (U.WM_XBUTTONDOWN,):
            self.tap_focus = inside
        if not inside:
            return False
        vr = mirrorwin.video_rect(hwnd, sw, sh)
        if vr is None or not vr.contains(x, y):
            return False
        sx, sy = screen_to_stream(x, y, vr, sw, sh)
        if msg in (U.WM_LBUTTONDOWN, U.WM_RBUTTONDOWN):
            if not cal.valid:
                self.on_event("need_calibration", None)
                return False
        if msg == U.WM_LBUTTONDOWN:
            self._tap_btn = P.BTN_LEFT
            self._tap_last = (x, y)
            self.ctl.tap_down(sx, sy)
            return True
        if msg == U.WM_RBUTTONDOWN:
            self.ctl.long_press(sx, sy)
            self._swallow_up.add(U.WM_RBUTTONUP)
            return True
        if msg in (U.WM_MOUSEWHEEL, U.WM_MOUSEHWHEEL) and cal.valid:
            self._wheel(msg, info, at=(sx, sy))
            return True
        return False

    # ---------------------------------------------------------------- keyboard hook
    def _kbd_hook(self, ncode, wparam, lparam):
        try:
            if ncode == U.HC_ACTION and self.enabled:
                k = ctypes.cast(lparam, ctypes.POINTER(U.KBDLLHOOKSTRUCT)).contents
                if not k.flags & U.LLKHF_INJECTED:
                    if self._handle_key(k):
                        return 1
        except Exception as e:
            self.on_event("log", f"keyboard hook: {e!r}")
        return U.CallNextHookEx(None, ncode, wparam, lparam)

    def _handle_key(self, k) -> bool:
        up = bool(k.flags & U.LLKHF_UP)
        vk = k.vkCode

        # ---- release / capture hotkey (pressed and released alone) ----
        # The key-up is swallowed only if the key-down was swallowed, so Windows never
        # ends up with a "stuck" Ctrl/Alt.
        if vk == self.hotkey_vk:
            if not up:
                if not self._hotkey_down:
                    self._hotkey_down = True
                    self._hotkey_clean = True
                    self._hotkey_swallowed = self.captured
                return self._hotkey_swallowed
            self._hotkey_down = False
            swallowed = self._hotkey_swallowed
            if self._hotkey_clean:
                if self.captured:
                    self.release_capture()
                elif self.mode != "off":
                    self.start_capture()
            return swallowed
        if self._hotkey_down:
            self._hotkey_clean = False

        forwarding = self.captured or (self.mode == "tap" and self.tap_focus and self._mirror_alive())
        if not forwarding:
            return False

        # AltGr sends a fake Left Ctrl first; ignore it.
        if vk == 0xA2 and k.scanCode & 0x200:
            return True

        usage_c = keymap.VK_TO_CONSUMER.get(vk)
        if usage_c is not None:
            if not up:
                self.ctl.consumer_tap(usage_c)
            return True

        usage = keymap.hid_usage(vk, k.scanCode & 0xFF, bool(k.flags & U.LLKHF_EXTENDED), self.swap_ctrl_cmd)
        if usage is None:
            return True
        changed = self._keys.release(usage) if up else self._keys.press(usage)
        if changed:
            mods, pressed = self._keys.report()
            self.ctl.keys(mods, pressed)
        return True

    def _mirror_alive(self) -> bool:
        hwnd, _, _ = self.get_mirror()
        if not hwnd:
            self.tap_focus = False
            return False
        return True
