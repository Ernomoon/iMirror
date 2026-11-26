"""Finding and arranging UxPlay's video window (Windows)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as W

from . import win32 as U


def find_window_for_pid(pid: int):
    """Largest visible top-level window owned by the process (GStreamer's video window)."""
    if not pid:
        return None
    found = []

    def cb(hwnd, _lparam):
        owner = W.DWORD(0)
        U.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and U.IsWindowVisible(hwnd):
            r = U.RECT()
            U.GetWindowRect(hwnd, ctypes.byref(r))
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if area > 0:
                found.append((area, hwnd))
        return True

    U.EnumWindows(U.WNDENUMPROC(cb), 0)
    if not found:
        return None
    found.sort(key=lambda t: t[0], reverse=True)
    return found[0][1]


def _monitor_rects(hwnd):
    mon = U.MonitorFromWindow(hwnd, U.MONITOR_DEFAULTTONEAREST)
    mi = U.MONITORINFO()
    mi.cbSize = ctypes.sizeof(U.MONITORINFO)
    U.GetMonitorInfoW(mon, ctypes.byref(mi))
    return mi.rcMonitor, mi.rcWork


def fit_to_screen(hwnd, src_w: int, src_h: int, fraction: float = 0.92):
    """Resize the window so the video is as large as possible (1:1 if it fits) without overflow."""
    if not hwnd or src_w <= 0 or src_h <= 0:
        return
    _, work = _monitor_rects(hwnd)
    ww, wh = work.right - work.left, work.bottom - work.top
    style = U.GetWindowLongPtrW(hwnd, U.GWL_STYLE) & 0xFFFFFFFF
    if style & U.WS_POPUP and not style & 0x00C00000:   # borderless fullscreen: leave alone
        return
    frame = U.RECT(0, 0, 100, 100)
    U.AdjustWindowRectEx(ctypes.byref(frame), style, False, 0)
    extra_w = (frame.right - frame.left) - 100
    extra_h = (frame.bottom - frame.top) - 100
    scale = min(1.0, (ww * fraction - extra_w) / src_w, (wh * fraction - extra_h) / src_h)
    cw, ch = int(src_w * scale), int(src_h * scale)
    tw, th = cw + extra_w, ch + extra_h
    x = work.left + (ww - tw) // 2
    y = work.top + (wh - th) // 2
    U.SetWindowPos(hwnd, None, x, y, tw, th, U.SWP_NOZORDER | U.SWP_NOACTIVATE)


def set_topmost(hwnd, on: bool):
    if hwnd:
        U.SetWindowPos(hwnd, U.HWND_TOPMOST if on else U.HWND_NOTOPMOST, 0, 0, 0, 0,
                       U.SWP_NOMOVE | U.SWP_NOSIZE | U.SWP_NOACTIVATE)


class FullscreenToggler:
    def __init__(self):
        self.saved = {}

    def toggle(self, hwnd) -> bool:
        if not hwnd:
            return False
        key = ctypes.cast(hwnd, ctypes.c_void_p).value if not isinstance(hwnd, int) else hwnd
        if key in self.saved:
            style, r = self.saved.pop(key)
            U.SetWindowLongPtrW(hwnd, U.GWL_STYLE, style)
            U.SetWindowPos(hwnd, None, r.left, r.top, r.right - r.left, r.bottom - r.top,
                           U.SWP_NOZORDER | U.SWP_FRAMECHANGED)
            return False
        style = U.GetWindowLongPtrW(hwnd, U.GWL_STYLE)
        r = U.RECT()
        U.GetWindowRect(hwnd, ctypes.byref(r))
        self.saved[key] = (style, r)
        mon, _ = _monitor_rects(hwnd)
        new_style = (style & ~U.WS_OVERLAPPEDWINDOW) | U.WS_POPUP | U.WS_VISIBLE
        U.SetWindowLongPtrW(hwnd, U.GWL_STYLE, ctypes.c_ssize_t(new_style & 0xFFFFFFFF).value)
        U.SetWindowPos(hwnd, None, mon.left, mon.top, mon.right - mon.left, mon.bottom - mon.top,
                       U.SWP_NOZORDER | U.SWP_FRAMECHANGED)
        return True
