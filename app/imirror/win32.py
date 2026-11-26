"""Thin, strictly-typed ctypes bindings for the Win32 APIs iMirror uses."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes as W

if sys.platform != "win32":  # pragma: no cover - allows importing on Linux for tests
    raise ImportError("win32 module is Windows-only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, WPARAM, LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, LPARAM)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0

WM_QUIT = 0x0012
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_INPUT = 0x00FF
WM_USER = 0x0400
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
WM_MOUSEHWHEEL = 0x020E

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80
LLMHF_INJECTED = 0x01

RIM_TYPEMOUSE = 0
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
MOUSE_MOVE_ABSOLUTE = 0x01

HWND_MESSAGE = W.HWND(-3)
HWND_TOPMOST = W.HWND(-1)
HWND_NOTOPMOST = W.HWND(-2)
GA_ROOT = 2
GWL_STYLE = -16
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
MONITOR_DEFAULTTONEAREST = 2


class POINT(ctypes.Structure):
    _fields_ = [("x", W.LONG), ("y", W.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", W.LONG), ("top", W.LONG), ("right", W.LONG), ("bottom", W.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", W.HWND), ("message", W.UINT), ("wParam", WPARAM), ("lParam", LPARAM),
                ("time", W.DWORD), ("pt", POINT), ("lPrivate", W.DWORD)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", W.USHORT), ("usUsage", W.USHORT), ("dwFlags", W.DWORD),
                ("hwndTarget", W.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", W.DWORD), ("dwSize", W.DWORD), ("hDevice", W.HANDLE), ("wParam", WPARAM)]


class _RMBTN(ctypes.Structure):
    _fields_ = [("usButtonFlags", W.USHORT), ("usButtonData", W.USHORT)]


class _RMU(ctypes.Union):
    _fields_ = [("ulButtons", W.ULONG), ("s", _RMBTN)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", W.USHORT), ("u", _RMU), ("ulRawButtons", W.ULONG),
                ("lLastX", W.LONG), ("lLastY", W.LONG), ("ulExtraInformation", W.ULONG)]


class RAWINPUT_MOUSE(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE),
                ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HICON)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", W.DWORD)]


def _sig(fn, res, *args):
    fn.restype = res
    fn.argtypes = list(args)
    return fn


SetWindowsHookExW = _sig(user32.SetWindowsHookExW, W.HHOOK, ctypes.c_int, HOOKPROC, W.HINSTANCE, W.DWORD)
UnhookWindowsHookEx = _sig(user32.UnhookWindowsHookEx, W.BOOL, W.HHOOK)
CallNextHookEx = _sig(user32.CallNextHookEx, LRESULT, W.HHOOK, ctypes.c_int, WPARAM, LPARAM)
GetMessageW = _sig(user32.GetMessageW, W.BOOL, ctypes.POINTER(MSG), W.HWND, W.UINT, W.UINT)
TranslateMessage = _sig(user32.TranslateMessage, W.BOOL, ctypes.POINTER(MSG))
DispatchMessageW = _sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(MSG))
PostThreadMessageW = _sig(user32.PostThreadMessageW, W.BOOL, W.DWORD, W.UINT, WPARAM, LPARAM)
PostMessageW = _sig(user32.PostMessageW, W.BOOL, W.HWND, W.UINT, WPARAM, LPARAM)
DefWindowProcW = _sig(user32.DefWindowProcW, LRESULT, W.HWND, W.UINT, WPARAM, LPARAM)
RegisterClassExW = _sig(user32.RegisterClassExW, W.ATOM, ctypes.POINTER(WNDCLASSEXW))
UnregisterClassW = _sig(user32.UnregisterClassW, W.BOOL, W.LPCWSTR, W.HINSTANCE)
CreateWindowExW = _sig(user32.CreateWindowExW, W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
DestroyWindow = _sig(user32.DestroyWindow, W.BOOL, W.HWND)
PostQuitMessage = _sig(user32.PostQuitMessage, None, ctypes.c_int)
RegisterRawInputDevices = _sig(user32.RegisterRawInputDevices, W.BOOL,
                               ctypes.POINTER(RAWINPUTDEVICE), W.UINT, W.UINT)
GetRawInputData = _sig(user32.GetRawInputData, W.UINT, W.HANDLE, W.UINT, W.LPVOID,
                       ctypes.POINTER(W.UINT), W.UINT)
GetModuleHandleW = _sig(kernel32.GetModuleHandleW, W.HMODULE, W.LPCWSTR)
GetCurrentThreadId = _sig(kernel32.GetCurrentThreadId, W.DWORD)
SetCursorPos = _sig(user32.SetCursorPos, W.BOOL, ctypes.c_int, ctypes.c_int)
GetCursorPos = _sig(user32.GetCursorPos, W.BOOL, ctypes.POINTER(POINT))
WindowFromPoint = _sig(user32.WindowFromPoint, W.HWND, POINT)
GetAncestor = _sig(user32.GetAncestor, W.HWND, W.HWND, W.UINT)
GetForegroundWindow = _sig(user32.GetForegroundWindow, W.HWND)
EnumWindows = _sig(user32.EnumWindows, W.BOOL, WNDENUMPROC, LPARAM)
GetWindowThreadProcessId = _sig(user32.GetWindowThreadProcessId, W.DWORD, W.HWND, ctypes.POINTER(W.DWORD))
IsWindowVisible = _sig(user32.IsWindowVisible, W.BOOL, W.HWND)
IsWindow = _sig(user32.IsWindow, W.BOOL, W.HWND)
GetClientRect = _sig(user32.GetClientRect, W.BOOL, W.HWND, ctypes.POINTER(RECT))
GetWindowRect = _sig(user32.GetWindowRect, W.BOOL, W.HWND, ctypes.POINTER(RECT))
ClientToScreen = _sig(user32.ClientToScreen, W.BOOL, W.HWND, ctypes.POINTER(POINT))
SetWindowPos = _sig(user32.SetWindowPos, W.BOOL, W.HWND, W.HWND, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, W.UINT)
GetWindowLongPtrW = _sig(user32.GetWindowLongPtrW, ctypes.c_ssize_t, W.HWND, ctypes.c_int)
SetWindowLongPtrW = _sig(user32.SetWindowLongPtrW, ctypes.c_ssize_t, W.HWND, ctypes.c_int, ctypes.c_ssize_t)
MonitorFromWindow = _sig(user32.MonitorFromWindow, W.HMONITOR, W.HWND, W.DWORD)
GetMonitorInfoW = _sig(user32.GetMonitorInfoW, W.BOOL, W.HMONITOR, ctypes.POINTER(MONITORINFO))
AdjustWindowRectEx = _sig(user32.AdjustWindowRectEx, W.BOOL, ctypes.POINTER(RECT), W.DWORD, W.BOOL, W.DWORD)
SetForegroundWindow = _sig(user32.SetForegroundWindow, W.BOOL, W.HWND)
GetDpiForWindow = None
try:
    GetDpiForWindow = _sig(user32.GetDpiForWindow, W.UINT, W.HWND)
except AttributeError:
    pass


def enable_dpi_awareness():
    """Per-monitor-v2 DPI awareness: all coordinates become real physical pixels."""
    try:
        fn = _sig(user32.SetProcessDpiAwarenessContext, W.BOOL, ctypes.c_void_p)
        if fn(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
