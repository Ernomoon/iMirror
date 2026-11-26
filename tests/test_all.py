"""iMirror test-suite (runs on any OS: Windows-only modules are not imported).

The firmware tests compile the REAL firmware/src/main.cpp on the host (tests/host stubs)
and feed it the exact bytes the app produces.
"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from imirror import protocol as P                      # noqa: E402
from imirror import keymap                             # noqa: E402
from imirror.calib import (Calibration, CalibrationSession, fit_rect,  # noqa: E402
                           screen_to_stream, Rect)
from imirror.receiver import build_args, parse_line, StreamInfo  # noqa: E402
from imirror.bridge import Controller                  # noqa: E402

HOST = ROOT / "tests" / "host"
HARNESS = HOST / ("harness.exe" if os.name == "nt" else "harness")


def build_harness():
    if HARNESS.exists() and HARNESS.stat().st_mtime > (ROOT / "firmware/src/main.cpp").stat().st_mtime:
        return
    subprocess.check_call(["g++", "-std=c++17", "-O1", "-I", str(HOST), "-o", str(HARNESS),
                           str(HOST / "harness.cpp")])


def run_firmware(data: bytes):
    """Returns list of (time_ms, report_id, bytes) the firmware notified."""
    out = subprocess.run([str(HARNESS)], input=data, capture_output=True, check=True).stdout.decode()
    reports = []
    for line in out.splitlines():
        parts = line.split()
        reports.append((int(parts[0]), int(parts[1]), bytes(int(x, 16) for x in parts[2:])))
    return reports


def s8(b):
    return b - 256 if b > 127 else b


class PointerSim:
    """iOS AssistiveTouch pointer model: clamped to the screen, optional acceleration."""

    def __init__(self, w, h, gain, accel=0.0, start=None):
        self.w, self.h, self.gain, self.accel = w, h, gain, accel
        self.x, self.y = start if start else (w / 2, h / 2)
        self.downs = []

    def factor(self, d):
        return 1.0 + self.accel * max(0, abs(d) - 10) / 117.0

    def run(self, reports):
        prev_btn = 0
        for _, rid, r in reports:
            if rid != 2:
                continue
            btn, dx, dy = r[0], s8(r[1]), s8(r[2])
            self.x = min(max(self.x + dx * self.gain * self.factor(dx), 0), self.w)
            self.y = min(max(self.y + dy * self.gain * self.factor(dy), 0), self.h)
            if btn & 1 and not prev_btn & 1:
                self.downs.append((self.x, self.y))
            prev_btn = btn
        return self


class FakeLink:
    def __init__(self):
        self.data = bytearray()
        self.events = []

    def write(self, b):
        self.data += b
        return True

    def on_event(self, k, v):
        self.events.append((k, v))


def drain(ctl, link):
    deadline = time.time() + 3
    while time.time() < deadline:
        time.sleep(0.05)
        if ctl.q.empty():
            time.sleep(0.05)
            break
    data = bytes(link.data)
    link.data.clear()
    return data


# ======================================================================================
class TestProtocol(unittest.TestCase):
    def test_roundtrip(self):
        data = P.move(10, -20, 1, -1, 50) + P.buttons(3) + P.keys(2, [4, 5]) + P.consumer(0x223) + P.delay(70000)
        frames = P.parse_frames(data)
        self.assertEqual([f[0] for f in frames], [1, 2, 3, 4, 5, 5])
        self.assertEqual(frames[0][1], bytes([10, 0, 0xEC, 0xFF, 1, 0xFF, 50]))
        self.assertEqual(frames[2][1], bytes([2, 4, 5, 0, 0, 0, 0]))

    def test_move_split_int16(self):
        frames = P.parse_frames(P.move(-70000, 5))
        self.assertEqual(len(frames), 3)
        import struct
        total = sum(struct.unpack("<hhbbB", f[1])[0] for f in frames)
        self.assertEqual(total, -70000)

    def test_zero_move_is_one_frame(self):
        self.assertEqual(len(P.parse_frames(P.move(0, 0))), 1)

    def test_checksum_rejects_corruption(self):
        good = bytearray(P.buttons(1))
        good[3] ^= 0xFF
        self.assertEqual(P.parse_frames(bytes(good)), [])


class TestKeymap(unittest.TestCase):
    def test_letters_and_specials(self):
        self.assertEqual(keymap.hid_usage(0x41, 0x1E, False), 0x04)      # A
        self.assertEqual(keymap.hid_usage(0x26, 0x48, True), 0x52)       # Up arrow
        self.assertEqual(keymap.hid_usage(0x0D, 0x1C, True), 0x58)       # keypad Enter
        self.assertEqual(keymap.hid_usage(0x0D, 0x1C, False), 0x28)      # Enter
        self.assertEqual(keymap.hid_usage(0x90, 0x45, True), 0x53)       # NumLock
        self.assertEqual(keymap.hid_usage(0x13, 0x45, False), 0x48)      # Pause

    def test_ctrl_cmd_swap(self):
        self.assertEqual(keymap.hid_usage(0xA2, 0x1D, False, swap_ctrl_cmd=True), 0xE3)
        self.assertEqual(keymap.hid_usage(0x5B, 0x5B, True, swap_ctrl_cmd=True), 0xE0)
        self.assertEqual(keymap.hid_usage(0xA2, 0x1D, False, swap_ctrl_cmd=False), 0xE0)

    def test_keystate(self):
        ks = keymap.KeyState()
        self.assertTrue(ks.press(0xE1))
        self.assertTrue(ks.press(0x04))
        self.assertFalse(ks.press(0x04))        # auto-repeat ignored
        self.assertEqual(ks.report(), (0x02, [0x04]))
        self.assertTrue(ks.release(0x04))
        self.assertTrue(ks.release(0xE1))
        self.assertEqual(ks.report(), (0, []))

    def test_text(self):
        strokes, layouts, skipped = keymap.text_to_strokes("Hi!\n")
        self.assertEqual(skipped, [])
        self.assertEqual(strokes, [(2, 0x0B), (0, 0x0C), (2, 0x1E), (0, 0x28)])
        self.assertEqual(layouts, {"us"})
        strokes, layouts, _ = keymap.text_to_strokes("Привет, Ёж.")
        self.assertEqual(strokes[0], (2, 0x0A))         # П = Shift + G-key
        self.assertIn((2, 0x38), strokes)                # comma = Shift + / key (ЙЦУКЕН)
        self.assertEqual(strokes[-1], (0, 0x38))         # period = / key (ЙЦУКЕН)
        self.assertIn((2, 0x35), strokes)                # Ё
        self.assertEqual(layouts, {"ru"})
        strokes, layouts, skipped = keymap.text_to_strokes("Цена: 5 руб. @")
        self.assertEqual(skipped, ["@"])                 # no @ on the Russian layout
        self.assertIn((2, 0x23), strokes)                # ':' = Shift+6 on ЙЦУКЕН
        _, _, skipped = keymap.text_to_strokes("a😀b")
        self.assertEqual(skipped, ["😀"])

    def test_tables_have_no_duplicate_chars_with_conflicts(self):
        for table in (keymap.US_TEXT, keymap.RU_TEXT):
            for ch, (u, m) in table.items():
                self.assertTrue(0 < u < 0xE8, ch)


class TestGeometry(unittest.TestCase):
    def test_fit_portrait_in_landscape(self):
        r = fit_rect(100, 50, 1600, 900, 1179, 2556)
        self.assertAlmostEqual(r.h, 900)
        self.assertAlmostEqual(r.x + r.w / 2, 100 + 800)
        sx, sy = screen_to_stream(r.x + r.w, r.y + r.h, r, 1179, 2556)
        self.assertAlmostEqual(sx, 1179)
        self.assertAlmostEqual(sy, 2556)

    def test_rect_contains(self):
        self.assertTrue(Rect(0, 0, 10, 10).contains(0, 9.9))
        self.assertFalse(Rect(0, 0, 10, 10).contains(10, 5))


class TestReceiver(unittest.TestCase):
    def test_args_max(self):
        cfg = {"width": 3840, "height": 2160, "refresh": 120, "max_fps": 120, "h265": True,
               "low_latency": True, "audio": True, "video_sink": "d3d12videosink",
               "server_name": "iMirror", "pin": ""}
        a = build_args(cfg, "uxplay.exe")
        self.assertEqual(a[a.index("-s") + 1], "3840x2160@120")
        self.assertEqual(a[a.index("-fps") + 1], "120")
        self.assertIn("-h265", a)
        self.assertEqual(a[a.index("-vsync") + 1], "no")
        self.assertEqual(a[a.index("-vs") + 1], "d3d12videosink")

    def test_args_limits(self):
        cfg = {"width": 3840, "height": 2160, "refresh": 256, "max_fps": 120, "h265": True,
               "server_name": "x", "pin": ""}
        with self.assertRaises(ValueError):
            build_args(cfg, "uxplay.exe")
        cfg.update(refresh=120, pin="0000")
        with self.assertRaises(ValueError):
            build_args(cfg, "uxplay.exe")
        cfg.update(pin="1234")
        a = build_args(cfg, "uxplay.exe")
        self.assertEqual(a[a.index("-pin") + 1], "1234")

    def test_parse(self):
        info = StreamInfo()
        self.assertEqual(parse_line("IMIRROR_CONN open=1", info), "conn")
        self.assertEqual(parse_line("IMIRROR_VIDEO codec=h265 w=1179 h=2556", info), "video")
        self.assertEqual(parse_line("IMIRROR_STATS fps=59.8 kbps=24000 frames=60", info), "stats")
        self.assertEqual((info.codec, info.width, info.height, info.fps), ("h265", 1179, 2556, 59.8))
        self.assertEqual(info.peak_fps, 59.8)
        self.assertIsNone(parse_line("random log", info))
        parse_line("IMIRROR_CONN open=0", info)
        self.assertEqual(info.fps, 0.0)


class TestReceiverProcess(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "uses a POSIX shell script as fake uxplay")
    def test_runs_and_parses_output(self):
        import tempfile, threading
        from imirror.receiver import Receiver
        d = tempfile.mkdtemp()
        fake = Path(d) / "uxplay"
        fake.write_text("#!/bin/sh\necho \"ARGS $*\"\necho IMIRROR_CONN open=1\n"
                        "echo IMIRROR_VIDEO codec=h265 w=1290 h=2796\n"
                        "printf 'IMIRROR_STATS fps=60.0 kbps=40000 frames=60\\r\\n'\nsleep 0.2\nexit 3\n")
        fake.chmod(0o755)
        events, done = [], threading.Event()

        def on(kind, val):
            events.append((kind, val if isinstance(val, str) else (val.width, val.fps)))
            if kind == "state" and str(val).startswith("stopped"):
                done.set()
        r = Receiver(on)
        cfg = {"uxplay_path": str(fake), "width": 3840, "height": 2160, "refresh": 120, "max_fps": 120,
               "h265": True, "low_latency": True, "audio": False, "video_sink": "d3d12videosink",
               "server_name": "iMirror", "pin": ""}
        r.start(cfg)
        self.assertTrue(done.wait(5))
        kinds = [e[0] for e in events]
        self.assertIn(("video", (1290, 0.0)), events)
        self.assertIn(("stats", (1290, 60.0)), events)
        self.assertTrue(any(k == "log" and "-s 3840x2160@120" in v for k, v in events if k == "log"))
        self.assertEqual(events[-1], ("state", "stopped:3"))
        self.assertIn("conn", kinds)


class TestCore(unittest.TestCase):
    def test_state_and_actions(self):
        import json, tempfile
        os.environ["APPDATA"] = tempfile.mkdtemp()
        from imirror.core import Core
        changes = []
        core = Core(on_change=lambda: changes.append(1))
        st = core.state()
        json.dumps(st, ensure_ascii=False)                 # must be JSON for the UI
        self.assertFalse(st["receiver"]["running"])
        self.assertEqual(core.dispatch("set", {"key": "preset", "value": "1440p"}), {"ok": True})
        self.assertEqual((core.cfg["width"], core.cfg["height"], core.cfg["h265"]), (2560, 1440, True))
        r = core.dispatch("action", {"name": "home"})      # no phone -> clean error, not exception
        self.assertFalse(r["ok"])
        self.assertTrue(core.state()["toasts"])
        with self.assertRaises(ValueError):
            core.dispatch("nope")
        self.assertIsInstance(core.dispatch("adapters"), list)
        core.shutdown()


class TestFirmware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_harness()

    def test_order_move_then_click(self):
        data = P.move(500, 0) + P.buttons(1) + P.move(0, 300) + P.buttons(0)
        reps = [r for r in run_firmware(data) if r[1] == 2]
        x = y = 0
        pressed_at = released_at = None
        for _, _, r in reps:
            if r[0] & 1 and pressed_at is None:
                pressed_at = (x, y)
            if pressed_at and not r[0] & 1 and released_at is None:
                released_at = (x, y)
            x += s8(r[1]); y += s8(r[2])
        self.assertEqual(pressed_at, (500, 0))
        self.assertEqual(released_at, (500, 300))
        self.assertEqual((x, y), (500, 300))

    def test_step_limit_and_pacing(self):
        reps = [r for r in run_firmware(P.move(100, -100, step=8)) if r[1] == 2]
        self.assertTrue(all(abs(s8(r[2][1])) <= 8 for r in reps))
        self.assertEqual(sum(s8(r[2][1]) for r in reps), 100)
        times = [r[0] for r in reps]
        self.assertTrue(all(b - a >= 8 for a, b in zip(times, times[1:])), times[:5])

    def test_button_state_is_resent(self):
        reps = [r for r in run_firmware(P.buttons(1) + P.delay(100) + P.buttons(0)) if r[1] == 2]
        states = [r[2][0] for r in reps]
        self.assertEqual(states.count(1), 2)          # press + re-send
        self.assertEqual(states.count(0), 2)          # release + re-send
        self.assertEqual(states[-1], 0)

    def test_keys_and_consumer(self):
        reps = run_firmware(P.keys(0x08, [0x2C]) + P.keys(0, []) + P.consumer(0xE9) + P.consumer(0))
        kb = [r[2] for r in reps if r[1] == 1]
        self.assertEqual(kb[0], bytes([0x08, 0, 0x2C, 0, 0, 0, 0, 0]))
        self.assertEqual(kb[-1], bytes(8))
        cons = [r[2] for r in reps if r[1] == 3]
        self.assertEqual(cons[0], bytes([0xE9, 0]))
        self.assertEqual(cons[-1], bytes(2))

    def test_garbage_and_bad_checksum_resync(self):
        bad = bytearray(P.buttons(1)); bad[-1] ^= 1
        data = b"\x00\xA5\xFF" + bytes(bad) + P.move(7, 0)
        reps = [r for r in run_firmware(data) if r[1] == 2]
        self.assertEqual(sum(s8(r[2][1]) for r in reps), 7)
        self.assertTrue(all(r[2][0] == 0 for r in reps))

    def test_release_clears(self):
        reps = [r for r in run_firmware(P.buttons(1) + P.move(3000, 0) + P.release()) if r[1] == 2]
        self.assertEqual(reps[-1][2][0], 0)


class TestHidEngineTwin(unittest.TestCase):
    """The Python HID scheduler (built-in Bluetooth path) must match the firmware exactly."""

    @classmethod
    def setUpClass(cls):
        build_harness()

    @staticmethod
    def run_python(data: bytes):
        from imirror.hid_engine import HidEngine
        eng = HidEngine()
        eng.set_connected(True)
        eng.feed(data, now_ms=1000)
        out = []
        t = 1_000_000
        for _ in range(120001):
            for rid, rep in eng.tick(t):
                out.append((t // 1000, rid, bytes(rep)))
            t += 250
        return out

    def compare(self, data):
        fw = run_firmware(data)
        py = self.run_python(data)
        self.assertEqual(len(fw), len(py), (fw[:6], py[:6]))
        for a, b in zip(fw, py):
            self.assertEqual(a, b)

    def test_move_click_sequence(self):
        self.compare(P.move(500, -260, 3, -1) + P.buttons(1) + P.move(0, 300, step=20) + P.buttons(0))

    def test_long_press_and_keys(self):
        self.compare(P.move(-6000, -6000) + P.delay(40) + P.move(123, 456, step=8) + P.buttons(1)
                     + P.delay(650) + P.buttons(0) + P.keys(0x08, [0x2C]) + P.keys(0, [])
                     + P.consumer(0xE9) + P.delay(60) + P.consumer(0))

    def test_text_typing(self):
        strokes, _, _ = keymap.text_to_strokes("Hello, iPhone!")
        data = b"".join(P.keys(m, [u]) + P.keys(0, []) for m, u in strokes)
        self.compare(data)

    def test_corruption(self):
        bad = bytearray(P.buttons(1)); bad[-1] ^= 1
        self.compare(b"\x00\xA5\xFF" + bytes(bad) + P.move(7, 0) + P.clear_move() + P.move(3, 3))


class TestTapAccuracy(unittest.TestCase):
    """End-to-end: Controller -> real firmware -> simulated iPhone pointer."""

    W, H = 1179, 2556

    @classmethod
    def setUpClass(cls):
        build_harness()

    def calibrate(self, ctl, link, gain, accel):
        sess = CalibrationSession(self.W, self.H)
        while not sess.finished():
            counts, step = sess.current()
            ctl.calib_probe(counts, step)
            sim = PointerSim(self.W, self.H, gain, accel, start=(300, 900)).run(run_firmware(drain(ctl, link)))
            sess.add_click(round(sim.x), round(sim.y))       # user clicks to the nearest pixel
        cal, report = sess.result()
        ctl.calibration = cal
        return cal, report

    def check(self, gain, accel, expect_step):
        link = FakeLink()
        ctl = Controller(link)
        ctl.src = (self.W, self.H)
        cal, report = self.calibrate(ctl, link, gain, accel)
        self.assertEqual(cal.step, expect_step, report)
        targets = [(10, 10), (self.W / 2, self.H / 2), (1100, 2500), (200, 2300), (900, 150)]
        for tx, ty in targets:
            ctl.tap_down(tx, ty)
            ctl.buttons(0)
            sim = PointerSim(self.W, self.H, gain, accel, start=(800, 400)).run(run_firmware(drain(ctl, link)))
            self.assertEqual(len(sim.downs), 1)
            px, py = sim.downs[0]
            err = max(abs(px - tx), abs(py - ty))
            # one HID count is `gain` pixels; allow 1 count + calibration click rounding
            self.assertLessEqual(err, gain * 1.5 + 3, f"target {tx},{ty} -> {px:.1f},{py:.1f}\n{report}")

    def test_linear_pointer(self):
        self.check(gain=2.3, accel=0.0, expect_step=127)

    def test_accelerated_pointer(self):
        self.check(gain=1.7, accel=1.5, expect_step=CalibrationSession.SMALL)

    def test_swipe_drag(self):
        link = FakeLink()
        ctl = Controller(link)
        ctl.src = (self.W, self.H)
        ctl.calibration = Calibration(2.0 / 2556, 2.0 / 2556, 127, True)
        ctl.tap_down(600, 2000)
        for _ in range(100):
            ctl.drag_px(0, -15.37)                  # swipe up 1537 px in small steps
        ctl.buttons(0)
        sim = PointerSim(self.W, self.H, 2.0, start=(50, 50))
        reps = run_firmware(drain(ctl, link))
        sim.run(reps)
        self.assertAlmostEqual(sim.downs[0][1], 2000, delta=2)
        self.assertAlmostEqual(sim.y, 2000 - 1537, delta=2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
