"""Windows input capture: the PC mouse + keyboard drive the iPhone (like a virtual machine).

Click inside the mirror window (or press the hotkey, Right Ctrl) and mouse + keyboard go to
the iPhone; the iPhone's pointer moves with raw mouse counts (no Windows acceleration, never
stuck at a screen edge). Press Right Ctrl again to get the PC back.

WH_MOUSE_LL / WH_KEYBOARD_LL hooks decide what is swallowed; movement comes from Raw Input.
If Raw Input is silent, movement is computed from hook coordinates around a parked cursor.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as W

from . import win32 as U
from . import keymap
from . import mirrorwin

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_BACK, BTN_FORWARD = 1, 2, 4, 8, 16
HOTKEY_VK = 0xA3          # Right Ctrl
_BTN_DOWN = {U.WM_LBUTTONDOWN: BTN_LEFT, U.WM_RBUTTONDOWN: BTN_RIGHT, U.WM_MBUTTONDOWN: BTN_MIDDLE}
_BTN_UP = {U.WM_LBUTTONUP: BTN_LEFT, U.WM_RBUTTONUP: BTN_RIGHT, U.WM_MBUTTONUP: BTN_MIDDLE}


def _hiword_signed(v: int) -> int:
    return ctypes.c_short((v >> 16) & 0xFFFF).value


class InputCapture:
    def __init__(self, hid, get_mirror_hwnd, on_event):
        """hid: BleHid; get_mirror_hwnd() -> hwnd|None; on_event(kind, value) from the hook thread."""
        self.hid = hid
        self.get_mirror = get_mirror_hwnd
        self.on_event = on_event
        self.sensitivity = 1.0
        self.wheel_mult = 1.0
        self.invert_wheel = False
        self.swap_ctrl_cmd = True
        self.click_to_capture = True
        self.enabled = True
        self.captured = False
        self._keys = keymap.KeyState()
        self._buttons = 0
        self._hot_down = self._hot_clean = self._hot_swallowed = False
        self._raw_seen = 0.0
        self._park = (0, 0)
        self._swallow_up = set()
        self._wheel_acc = 0.0
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="iMirror-input", daemon=True)
        self._thread.start()
        self._ready.wait(3)

    # ---------------------------------------------------------------- public
    def start_capture(self) -> bool:
        if not self.enabled or self.captured:
            return False
        hwnd = self.get_mirror()
        rect = mirrorwin.client_rect_screen(hwnd) if hwnd else None
        if rect:
            self._park = (int(rect[0] + rect[2] / 2), int(rect[1] + rect[3] / 2))
        else:
            pt = U.POINT()
            U.GetCursorPos(ctypes.byref(pt))
            self._park = (pt.x, pt.y)
        U.SetCursorPos(*self._park)
        self._keys.clear()
        self._buttons = 0
        self.hid.release_all()
        self.captured = True
        self.on_event("capture", True)
        return True

    def release_capture(self):
        if not self.captured:
            return
        self.captured = False
        self._keys.clear()
        self._buttons = 0
        self.hid.release_all()
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
                self.on_event("log", "Raw Input недоступен: движение считается по курсору")
        self._mouse_proc = U.HOOKPROC(self._mouse_hook)
        self._kbd_proc = U.HOOKPROC(self._kbd_hook)
        self._mh = U.SetWindowsHookExW(U.WH_MOUSE_LL, self._mouse_proc, hinst, 0)
        self._kh = U.SetWindowsHookExW(U.WH_KEYBOARD_LL, self._kbd_proc, hinst, 0)
        if not self._mh or not self._kh:
            self.on_event("log", f"Не удалось перехватить ввод (ошибка {ctypes.get_last_error()})")
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

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == U.WM_INPUT and self.captured:
            try:
                ri = U.RAWINPUT_MOUSE()
                size = W.UINT(ctypes.sizeof(ri))
                got = U.GetRawInputData(W.HANDLE(lparam), U.RID_INPUT, ctypes.byref(ri), ctypes.byref(size),
                                        ctypes.sizeof(U.RAWINPUTHEADER))
                if got not in (0, 0xFFFFFFFF) and ri.header.dwType == U.RIM_TYPEMOUSE:
                    m = ri.mouse
                    if not m.usFlags & U.MOUSE_MOVE_ABSOLUTE:
                        self._raw_seen = time.monotonic()
                        if m.lLastX or m.lLastY:
                            self.hid.move(m.lLastX * self.sensitivity, m.lLastY * self.sensitivity)
            except Exception as e:
                self.on_event("log", f"raw input: {e!r}")
        return U.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------------------------------------------------------------- mouse
    def _mouse_hook(self, ncode, wparam, lparam):
        try:
            if ncode == U.HC_ACTION and self.enabled:
                info = ctypes.cast(lparam, ctypes.POINTER(U.MSLLHOOKSTRUCT)).contents
                if not info.flags & U.LLMHF_INJECTED and self._handle_mouse(int(wparam), info):
                    return 1
        except Exception as e:
            self.on_event("log", f"mouse hook: {e!r}")
        return U.CallNextHookEx(None, ncode, wparam, lparam)

    def _handle_mouse(self, msg, info) -> bool:
        if msg in self._swallow_up:
            self._swallow_up.discard(msg)
            return True
        if self.captured:
            return self._captured_mouse(msg, info)
        if self.click_to_capture and msg == U.WM_LBUTTONDOWN and self.hid.connected:
            hwnd = self.get_mirror()
            if hwnd and mirrorwin.same_hwnd(mirrorwin.window_under(info.pt.x, info.pt.y), hwnd):
                if self.start_capture():
                    self._swallow_up.add(U.WM_LBUTTONUP)
                    return True
        return False

    def _captured_mouse(self, msg, info) -> bool:
        if msg == U.WM_MOUSEMOVE:
            if time.monotonic() - self._raw_seen > 0.25:
                dx, dy = info.pt.x - self._park[0], info.pt.y - self._park[1]
                if dx or dy:
                    self.hid.move(dx * self.sensitivity, dy * self.sensitivity)
            return True
        if msg in _BTN_DOWN:
            self._buttons |= _BTN_DOWN[msg]
        elif msg in _BTN_UP:
            self._buttons &= ~_BTN_UP[msg]
        elif msg in (U.WM_XBUTTONDOWN, U.WM_XBUTTONUP):
            bit = BTN_BACK if (info.mouseData >> 16) & 0xFFFF == 1 else BTN_FORWARD
            self._buttons = self._buttons | bit if msg == U.WM_XBUTTONDOWN else self._buttons & ~bit
        elif msg == U.WM_MOUSEWHEEL:
            d = _hiword_signed(info.mouseData) / 120.0 * self.wheel_mult
            self._wheel_acc += -d if self.invert_wheel else d
            steps = int(self._wheel_acc)
            if steps:
                self._wheel_acc -= steps
                self.hid.scroll(steps)
            return True
        else:
            return True
        self.hid.buttons(self._buttons)
        return True

    # ---------------------------------------------------------------- keyboard
    def _kbd_hook(self, ncode, wparam, lparam):
        try:
            if ncode == U.HC_ACTION and self.enabled:
                k = ctypes.cast(lparam, ctypes.POINTER(U.KBDLLHOOKSTRUCT)).contents
                if not k.flags & U.LLKHF_INJECTED and self._handle_key(k):
                    return 1
        except Exception as e:
            self.on_event("log", f"keyboard hook: {e!r}")
        return U.CallNextHookEx(None, ncode, wparam, lparam)

    def _handle_key(self, k) -> bool:
        up = bool(k.flags & U.LLKHF_UP)
        vk = k.vkCode
        # Right Ctrl alone toggles capture; its key-up is swallowed only if its key-down was.
        if vk == HOTKEY_VK:
            if not up:
                if not self._hot_down:
                    self._hot_down, self._hot_clean, self._hot_swallowed = True, True, self.captured
                return self._hot_swallowed
            self._hot_down = False
            swallowed = self._hot_swallowed
            if self._hot_clean:
                if self.captured:
                    self.release_capture()
                elif self.hid.connected:
                    self.start_capture()
            return swallowed
        if self._hot_down:
            self._hot_clean = False
        if not self.captured:
            return False
        if vk == 0xA2 and k.scanCode & 0x200:      # AltGr's fake Left Ctrl
            return True
        usage_c = keymap.VK_TO_CONSUMER.get(vk)
        if usage_c is not None:
            if not up:
                self.hid.consumer_tap(usage_c)
            return True
        usage = keymap.hid_usage(vk, k.scanCode & 0xFF, bool(k.flags & U.LLKHF_EXTENDED), self.swap_ctrl_cmd)
        if usage is None:
            return True
        changed = self._keys.release(usage) if up else self._keys.press(usage)
        if changed:
            mods, pressed = self._keys.report()
            self.hid.keys(mods, pressed)
        return True
