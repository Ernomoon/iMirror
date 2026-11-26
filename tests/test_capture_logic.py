"""Logic tests for the Windows input engine, run on any OS with an emulated Win32 layer.

Hooks cannot be installed outside Windows, so the hook handlers are called directly with
synthetic MSLLHOOKSTRUCT / KBDLLHOOKSTRUCT events.
"""
import ctypes
import sys
import unittest
from ctypes import wintypes as W
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

if sys.platform != "win32":
    # Emulate Windows (LLP64) type sizes and DLLs so the modules import.
    for n, t in dict(LONG=ctypes.c_int32, ULONG=ctypes.c_uint32, DWORD=ctypes.c_uint32, BOOL=ctypes.c_int32,
                     UINT=ctypes.c_uint32, USHORT=ctypes.c_uint16, ATOM=ctypes.c_uint16).items():
        setattr(W, n, t)

    class _Fn:
        def __init__(self, name):
            self.name, self.restype, self.argtypes = name, None, None

        def __call__(self, *a):
            return 1

    class _DLL:
        def __getattr__(self, n):
            f = _Fn(n)
            setattr(self, n, f)
            return f

    ctypes.WinDLL = lambda *a, **k: _DLL()
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    _real_platform = sys.platform
    sys.platform = "win32"
    from imirror import win32 as U          # noqa: E402
    from imirror import capture, mirrorwin  # noqa: E402
    sys.platform = _real_platform
else:
    from imirror import win32 as U          # noqa: E402
    from imirror import capture, mirrorwin  # noqa: E402

from imirror import protocol as P            # noqa: E402
from imirror.calib import Calibration, Rect  # noqa: E402

MIRROR = 0x1234


class FakeCtl:
    def __init__(self):
        self.calls = []
        self.calibration = Calibration(1 / 1000, 1 / 1000, 127, True)

    def __getattr__(self, name):
        def rec(*a):
            self.calls.append((name,) + a)
        return rec


def mouse(msg, x, y, data=0, injected=False):
    m = U.MSLLHOOKSTRUCT()
    m.pt.x, m.pt.y = x, y
    m.mouseData = data
    m.flags = U.LLMHF_INJECTED if injected else 0
    return msg, m


def key(vk, scan, up=False, ext=False):
    k = U.KBDLLHOOKSTRUCT()
    k.vkCode, k.scanCode = vk, scan
    k.flags = (U.LLKHF_UP if up else 0) | (U.LLKHF_EXTENDED if ext else 0)
    return k


class EngineTest(unittest.TestCase):
    def setUp(self):
        # Don't start the real hook thread.
        self._thr = capture.threading.Thread
        capture.threading.Thread = lambda *a, **k: type("T", (), {"start": lambda s: None})()
        capture.U.SetCursorPos = lambda x, y: True
        mirrorwin.window_under = lambda x, y: MIRROR if (0 <= x < 1000 and 0 <= y < 1000) else 0x9999
        capture.mirrorwin.client_rect_screen = lambda h: Rect(0, 0, 1000, 1000)
        capture.mirrorwin.video_rect = lambda h, w, hh: Rect(250, 0, 500, 1000)   # letterboxed phone
        self.ctl = FakeCtl()
        self.events = []
        cfg = {"control_mode": "capture", "release_hotkey": "rctrl", "swap_ctrl_cmd": True}
        capture.InputEngine._ready = None
        self.eng = capture.InputEngine.__new__(capture.InputEngine)
        # run __init__ without waiting on the thread
        orig_event = capture.threading.Event
        capture.threading.Event = lambda: type("E", (), {"wait": lambda s, t=None: True,
                                                         "set": lambda s: None})()
        try:
            capture.InputEngine.__init__(self.eng, self.ctl, lambda: (MIRROR, 500, 1000),
                                         lambda k, v: self.events.append((k, v)), cfg)
        finally:
            capture.threading.Event = orig_event
        self.eng.enabled = True

    def tearDown(self):
        capture.threading.Thread = self._thr

    def m(self, *args, **kw):
        return self.eng._handle_mouse(*mouse(*args, **kw))

    # ------------------------------------------------------------------ capture mode
    def test_click_in_mirror_starts_capture_and_swallows(self):
        self.assertTrue(self.m(U.WM_LBUTTONDOWN, 500, 500))
        self.assertTrue(self.eng.captured)
        self.assertTrue(self.m(U.WM_LBUTTONUP, 500, 500))       # its up is swallowed too
        self.assertNotIn(("buttons", 0), self.ctl.calls)          # and not forwarded as a tap

    def test_click_outside_mirror_passes(self):
        self.assertFalse(self.m(U.WM_LBUTTONDOWN, 1500, 500))
        self.assertFalse(self.eng.captured)

    def test_captured_buttons_and_wheel(self):
        self.eng.start_capture()
        self.assertTrue(self.m(U.WM_LBUTTONDOWN, 1, 1))
        self.assertTrue(self.m(U.WM_RBUTTONDOWN, 1, 1))
        self.assertTrue(self.m(U.WM_LBUTTONUP, 1, 1))
        self.assertTrue(self.m(U.WM_XBUTTONDOWN, 1, 1, data=2 << 16))
        self.assertTrue(self.m(U.WM_MOUSEWHEEL, 1, 1, data=(-240 & 0xFFFF) << 16))
        btns = [c[1] for c in self.ctl.calls if c[0] == "buttons"]
        self.assertEqual(btns, [P.BTN_LEFT, P.BTN_LEFT | P.BTN_RIGHT, P.BTN_RIGHT,
                                P.BTN_RIGHT | P.BTN_FORWARD])
        self.assertIn(("scroll", -2.0, 0.0), self.ctl.calls)

    def test_hook_delta_fallback_when_no_raw_input(self):
        self.eng.start_capture()
        px, py = self.eng._park
        self.assertTrue(self.m(U.WM_MOUSEMOVE, px + 5, py - 3))
        self.assertIn(("move", 5.0, -3.0), self.ctl.calls)

    def test_injected_events_ignored(self):
        self.eng.start_capture()
        # injected events are filtered in _mouse_hook before _handle_mouse; emulate that path
        msg, m = mouse(U.WM_MOUSEMOVE, 10, 10, injected=True)
        self.assertTrue(m.flags & U.LLMHF_INJECTED)

    # ------------------------------------------------------------------ hotkey
    def test_hotkey_toggles_without_stuck_ctrl(self):
        k = self.eng._handle_key
        # not captured: RCtrl down/up pass through to Windows and start capture
        self.assertFalse(k(key(0xA3, 0x1D, ext=True)))
        self.assertFalse(k(key(0xA3, 0x1D, up=True, ext=True)))
        self.assertTrue(self.eng.captured)
        # captured: RCtrl down/up are swallowed and release capture
        self.assertTrue(k(key(0xA3, 0x1D, ext=True)))
        self.assertTrue(k(key(0xA3, 0x1D, up=True, ext=True)))
        self.assertFalse(self.eng.captured)

    def test_hotkey_combo_does_not_toggle(self):
        k = self.eng._handle_key
        k(key(0xA3, 0x1D, ext=True))
        k(key(0x43, 0x2E))                       # RCtrl+C on the PC
        k(key(0x43, 0x2E, up=True))
        k(key(0xA3, 0x1D, up=True, ext=True))
        self.assertFalse(self.eng.captured)

    def test_keys_forwarded_with_ctrl_as_cmd(self):
        self.eng.start_capture()
        k = self.eng._handle_key
        self.assertTrue(k(key(0xA2, 0x1D)))      # LCtrl -> LGUI (⌘)
        self.assertTrue(k(key(0x43, 0x2E)))      # C
        self.assertTrue(k(key(0x43, 0x2E)))      # auto-repeat: no new report
        self.assertTrue(k(key(0x43, 0x2E, up=True)))
        self.assertTrue(k(key(0xA2, 0x1D, up=True)))
        reports = [c[1:] for c in self.ctl.calls if c[0] == "keys"]
        self.assertEqual(reports, [(0x08, []), (0x08, [0x06]), (0x08, []), (0, [])])

    def test_altgr_fake_ctrl_ignored(self):
        self.eng.start_capture()
        self.assertTrue(self.eng._handle_key(key(0xA2, 0x21D)))
        self.assertFalse([c for c in self.ctl.calls if c[0] == "keys"])

    def test_keys_not_forwarded_when_not_captured(self):
        self.assertFalse(self.eng._handle_key(key(0x41, 0x1E)))

    def test_release_capture_releases_everything(self):
        self.eng.start_capture()
        self.eng._handle_key(key(0x41, 0x1E))
        self.eng.release_capture()
        self.assertEqual(self.ctl.calls[-1], ("release_all",))

    # ------------------------------------------------------------------ tap mode
    def test_tap_mode(self):
        self.eng.set_mode("tap")
        # click inside the letterboxed phone image (x 250..750)
        self.assertTrue(self.m(U.WM_LBUTTONDOWN, 500, 250))
        self.assertIn(("tap_down", 250.0, 250.0), self.ctl.calls)
        self.assertFalse(self.m(U.WM_MOUSEMOVE, 510, 200))     # cursor still moves on the PC
        self.assertIn(("drag_px", 10.0, -50.0), self.ctl.calls)
        self.assertTrue(self.m(U.WM_LBUTTONUP, 510, 200))
        self.assertEqual(self.ctl.calls[-1], ("buttons", 0))
        # click on the black bar (outside image) passes through
        self.assertFalse(self.m(U.WM_LBUTTONDOWN, 100, 100))
        self.assertFalse(self.eng.captured)

    def test_tap_keyboard_focus(self):
        self.eng.set_mode("tap")
        self.m(U.WM_LBUTTONDOWN, 500, 250); self.m(U.WM_LBUTTONUP, 500, 250)
        self.assertTrue(self.eng._handle_key(key(0x41, 0x1E)))  # goes to iPhone
        self.m(U.WM_LBUTTONDOWN, 1500, 250)                     # click elsewhere on the PC
        self.assertFalse(self.eng._handle_key(key(0x42, 0x30)))

    def test_tap_requires_calibration(self):
        self.eng.set_mode("tap")
        self.ctl.calibration = Calibration()
        self.assertFalse(self.m(U.WM_LBUTTONDOWN, 500, 250))
        self.assertIn(("need_calibration", None), self.events)

    def test_calibration_click(self):
        self.eng.set_mode("off")
        self.eng.calib_click = True
        self.assertTrue(self.m(U.WM_LBUTTONDOWN, 400, 300))
        self.assertTrue(self.m(U.WM_LBUTTONUP, 400, 300))
        self.assertIn(("calib_click", (400, 300)), self.events)
        self.assertIsNone(self.eng.calib_click)
        self.assertFalse(self.m(U.WM_LBUTTONDOWN, 400, 300))   # next click is normal again

    def test_disabled_engine_cannot_capture(self):
        self.eng.enabled = False
        self.assertFalse(self.eng.start_capture())


if __name__ == "__main__":
    unittest.main(verbosity=2)
