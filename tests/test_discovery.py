"""End-to-end discovery test: patched UxPlay + iMirror's announcer, seen like an iPhone sees it.

1. start uxplay with IMIRROR_EXTERNAL_MDNS=1 (it prints service data, no own responder)
2. iMirror's Advertiser announces it on the LAN adapters
3. a separate zeroconf "client" browses _airplay._tcp, resolves it and asks GET /info
   over the advertised address/port — exactly the first steps an iPhone performs.

Usage:  python tests/test_discovery.py <path-to-uxplay[.exe]>
"""
import plistlib
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from imirror.mdns import Advertiser, list_adapters, choose_addresses  # noqa: E402
from imirror.receiver import receiver_env  # noqa: E402


def http_get_info(ip, port):
    s = socket.create_connection((ip, port), timeout=5)
    s.sendall(b"GET /info RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: AirPlay/750.0\r\n\r\n")
    data = b""
    deadline = time.time() + 5
    while time.time() < deadline:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
        head, sep, body = data.partition(b"\r\n\r\n")
        if sep:
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            if len(body) >= length:
                return head.split(b"\r\n")[0], body[:length]
    raise RuntimeError(f"no full response: {data[:200]!r}")


def main(uxplay):
    from zeroconf import ServiceBrowser, Zeroconf, IPVersion

    adapters = list_adapters()
    print("adapters:", [(a.name, a.ip, a.virtual, a.reasons) for a in adapters])
    addrs = choose_addresses(adapters)
    print("announce on:", addrs)

    env = receiver_env(uxplay)          # bundled GStreamer + IMIRROR_EXTERNAL_MDNS=1
    proc = subprocess.Popen([uxplay, "-n", "iMirror Test", "-nh", "-vs", "fakesink", "-as", "0"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    events = []
    ready = threading.Event()
    adv = Advertiser(lambda k, v: (events.append((k, v)), ready.set()))

    def pump():
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            print("uxplay>", line[:160])
            adv.feed(line)
    threading.Thread(target=pump, daemon=True).start()

    try:
        assert ready.wait(15), "uxplay printed no IMIRROR_MDNS lines"
        assert events[-1][1].get("ok"), events
        found = {}
        got = threading.Event()

        class L:
            def add_service(self, zc, t, name):
                info = zc.get_service_info(t, name, timeout=3000)
                if info and "iMirror Test" in name:
                    found[t] = info
                    if len(found) == 2:
                        got.set()

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass

        client = Zeroconf(ip_version=IPVersion.V4Only)
        ServiceBrowser(client, ["_airplay._tcp.local.", "_raop._tcp.local."], L())
        assert got.wait(15), f"iPhone-side browse did not find both services: {list(found)}"
        info = found["_airplay._tcp.local."]
        props = info.decoded_properties
        print("resolved:", info.name, info.parsed_addresses(), info.port, props.get("features"), props.get("model"))
        assert props.get("deviceid") and props.get("features") and props.get("pk"), props
        raop = found["_raop._tcp.local."]
        assert "@iMirror Test" in raop.name, raop.name
        status, body = http_get_info(info.parsed_addresses()[0], info.port)
        print("GET /info ->", status)
        assert b"200" in status, status
        plist = plistlib.loads(body)
        print("server info keys:", sorted(plist)[:8], "... width/height:",
              plist.get("displays", [{}])[0].get("widthPixels"), plist.get("displays", [{}])[0].get("heightPixels"))
        client.close()
        print("DISCOVERY E2E OK")
    finally:
        adv.stop()
        proc.terminate()


if __name__ == "__main__":
    main(sys.argv[1])
