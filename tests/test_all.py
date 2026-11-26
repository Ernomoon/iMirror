"""iMirror unit tests (any OS)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("APPDATA", tempfile.mkdtemp())
os.environ.setdefault("LOCALAPPDATA", os.environ["APPDATA"])

from imirror.receiver import build_args, parse_line, StreamInfo  # noqa: E402
from imirror import mdns  # noqa: E402


class TestReceiverArgs(unittest.TestCase):
    CFG = {"width": 3840, "height": 2160, "refresh": 120, "max_fps": 120, "h265": True, "low_latency": True,
           "audio": True, "video_sink": "d3d12videosink", "server_name": "iMirror", "pin": ""}

    def test_max(self):
        a = build_args(dict(self.CFG), "uxplay.exe")
        self.assertEqual(a[a.index("-s") + 1], "3840x2160@120")
        self.assertEqual(a[a.index("-fps") + 1], "120")
        self.assertIn("-h265", a)
        self.assertEqual(a[a.index("-vsync") + 1], "no")
        self.assertIn("-nofreeze", a)
        self.assertRegex(a[a.index("-m") + 1], r"^02(:[0-9a-f]{2}){5}$")

    def test_ports_and_name(self):
        cfg = dict(self.CFG, server_name="Мой ПК  дома", _ports=((7100, 7000, 7001), (6000, 6001, 7011)),
                   device_id="02:aa:bb:cc:dd:ee")
        a = build_args(cfg, "uxplay.exe")
        self.assertEqual(a[a.index("-n") + 1], "Мой-ПК-дома")
        self.assertEqual(a[a.index("-m") + 1], "02:aa:bb:cc:dd:ee")
        i = a.index("-p")
        self.assertEqual(a[i:i + 6], ["-p", "tcp", "7100,7000,7001", "-p", "udp", "6000,6001,7011"])
        cfg.update(software_decode=True)
        a = build_args(cfg, "uxplay.exe")
        self.assertIn("avdec_h264 output-corrupt=false", a[a.index("-vd") + 1])

    def test_limits(self):
        cfg = dict(self.CFG, refresh=256)
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
        parse_line("IMIRROR_CONN open=0", info)
        self.assertEqual(info.fps, 0.0)


class TestNetif(unittest.TestCase):
    def test_txt_and_best(self):
        from imirror import netif
        self.assertEqual(netif.parse_txt(b"\x05a=1,2\x04pk=x\x00"), [("a", "1,2"), ("pk", "x")])
        I = netif.Iface
        wifi = I(5, "Беспроводная сеть", "Intel Wi-Fi 6", netif.IF_TYPE_IEEE80211, [("192.168.1.5", 24)], True, 35)
        vpn = I(9, "AmneziaWG", "AmneziaWG Tunnel", netif.IF_TYPE_ETHERNET, [("10.8.1.2", 32)], True, 5)
        veth = I(7, "vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", netif.IF_TYPE_ETHERNET, [("172.20.0.1", 20)], False, 15)
        self.assertIs(netif.best_interface([vpn, veth, wifi]), wifi)
        self.assertIs(netif.best_interface([vpn, veth, wifi], "172.20.0.1"), veth)


class TestMdns(unittest.TestCase):
    def test_parse_line(self):
        s = mdns.parse_mdns_line("IMIRROR_MDNS type=_raop._tcp port=7000 name=AABB@Мой ПК._raop._tcp.local txt=0361623d")
        self.assertEqual((s.type, s.port, s.instance, s.txt), ("_raop._tcp", 7000, "AABB@Мой ПК", b"\x03ab="))
        self.assertIsNone(mdns.parse_mdns_line("random"))

    def test_choose(self):
        A = mdns.Adapter
        ads = [A("Wi-Fi", "192.168.1.5", False), A("AmneziaWG", "10.8.1.2", True, reasons=["vpn"]),
               A("vEthernet", "172.20.0.1", True)]
        self.assertEqual(mdns.choose_addresses(ads), ["192.168.1.5"])
        self.assertEqual(mdns.choose_addresses(ads, "10.8.1.2"), ["10.8.1.2"])
        self.assertEqual(mdns.choose_addresses([A("x", "169.254.1.1", True), A("y", "10.0.0.2", True)]), ["10.0.0.2"])

    def test_vpn_hints(self):
        for name in ("AmneziaWG Tunnel", "WireGuard Tunnel", "Wintun Userspace Tunnel", "Hyper-V Virtual Ethernet"):
            self.assertTrue(any(h in name.lower() for h in mdns.VIRTUAL_HINTS), name)

    def test_bonjour_absent_off_windows(self):
        if sys.platform != "win32":
            self.assertIsNone(mdns.Bonjour.load())


class TestReceiverProcess(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "uses a POSIX shell script as fake uxplay")
    def test_runs_and_parses_output(self):
        from imirror.receiver import Receiver
        d = tempfile.mkdtemp()
        fake = Path(d) / "uxplay"
        fake.write_text("#!/bin/sh\necho \"ARGS $*\"\necho IMIRROR_CONN open=1\n"
                        "echo IMIRROR_VIDEO codec=h265 w=1290 h=2796\n"
                        "printf 'IMIRROR_STATS fps=60.0 kbps=40000 frames=60\\r\\n'\n"
                        "echo 'IMIRROR_MDNS type=_airplay._tcp port=7000 name=x._airplay._tcp.local txt=00'\n"
                        "sleep 0.2\nexit 3\n")
        fake.chmod(0o755)
        events, hooked, done = [], [], threading.Event()

        def on(kind, val):
            events.append((kind, val if isinstance(val, str) else (val.width, val.fps)))
            if kind == "state" and str(val).startswith("stopped"):
                done.set()
        r = Receiver(on, line_hook=lambda line: line.startswith("IMIRROR_MDNS") and not hooked.append(line))
        cfg = dict(TestReceiverArgs.CFG, uxplay_path=str(fake), audio=False)
        r.start(cfg)
        self.assertTrue(done.wait(5))
        self.assertIn(("video", (1290, 0.0)), events)
        self.assertIn(("stats", (1290, 60.0)), events)
        self.assertEqual(len(hooked), 1)
        self.assertEqual(events[-1], ("state", "stopped:3"))


class TestCore(unittest.TestCase):
    def test_state_and_actions(self):
        from imirror.core import Core
        core = Core()
        st = core.state()
        json.dumps(st, ensure_ascii=False)
        self.assertEqual(st["phase"], "off")
        self.assertEqual(core.dispatch("set", {"key": "preset", "value": "1440p"}), {"ok": True})
        self.assertEqual((core.cfg["width"], core.cfg["height"], core.cfg["h265"]), (2560, 1440, True))
        with self.assertRaises(ValueError):
            core.dispatch("nope")
        self.assertIsInstance(core.dispatch("adapters"), list)
        core.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
