"""Look for an AirPlay receiver like an iPhone does (mDNS browse) and fetch GET /info.
Usage: python tests/browse.py [seconds]  -> exit 0 when an _airplay._tcp service answers."""
import socket
import sys
import time

from zeroconf import ServiceBrowser, Zeroconf

found = {}


class L:
    def add_service(self, zc, t, name):
        info = zc.get_service_info(t, name, 3000)
        if info:
            found[name] = (info.parsed_addresses(), info.port, info.properties)

    update_service = add_service

    def remove_service(self, *a):
        pass


zc = Zeroconf()
ServiceBrowser(zc, "_airplay._tcp.local.", L())
deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 60)
ok = False
while time.time() < deadline and not ok:
    time.sleep(1)
    for name, (addrs, port, props) in list(found.items()):
        for a in addrs:
            if ":" in a:
                continue
            try:
                s = socket.create_connection((a, port), timeout=5)
                s.sendall(b"GET /info RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: AirPlay/750.0\r\n\r\n")
                s.settimeout(5)
                head = s.recv(4096)
                s.close()
                print("found", name, a, port, "GET /info ->", head.split(b"\r\n")[0])
                ok = ok or head.startswith(b"RTSP/1.0 200")
            except Exception as e:
                print("found", name, a, port, "but /info failed:", e)
zc.close()
print("BROWSE OK" if ok else "BROWSE FAILED")
sys.exit(0 if ok else 1)
