"""AirPlay service announcement (mDNS / Bonjour) done by iMirror itself.

UxPlay's built-in responder (v1.74) announces on ONE network adapter only. On a PC with a
VPN, Hyper-V/WSL, VirtualBox or several network cards that is often the wrong one, and the
iPhone never sees the receiver. Here the patched UxPlay only prints its service data
(IMIRROR_MDNS lines) and python-zeroconf announces it on every real LAN adapter, answering
queries the way Apple devices expect.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import uuid
from dataclasses import dataclass, field

_LINE = re.compile(r"IMIRROR_MDNS type=(_\w+\._tcp) port=(\d+) name=(.+?) txt=([0-9a-fA-F]*)\s*$")

# Adapters that are never the Wi-Fi/Ethernet the iPhone is on.
VIRTUAL_HINTS = (
    "hyper-v", "vethernet", "wsl", "virtualbox", "vmware", "vmnet", "virtual", "loopback",
    "tap-", "tap ", "tun", "wintun", "wireguard", "openvpn", "tailscale", "zerotier", "hamachi",
    "radmin", "nordlynx", "protonvpn", "expressvpn", "amneziawg", "outline", "v2ray", "clash",
    "docker", "npcap", "bluetooth", "teredo", "isatap", "vpn",
)


@dataclass
class ServiceSpec:
    type: str            # "_airplay._tcp" / "_raop._tcp"
    port: int
    instance: str        # "iMirror" / "AABBCCDDEEFF@iMirror"
    txt: bytes


@dataclass
class Adapter:
    name: str
    ip: str
    virtual: bool
    prefix: int = 24
    reasons: list = field(default_factory=list)


def parse_mdns_line(line: str) -> ServiceSpec | None:
    m = _LINE.search(line)
    if not m:
        return None
    stype, port, full, txt = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    suffix = f".{stype}.local"
    instance = full[:-len(suffix)] if full.endswith(suffix) else full.split(".")[0]
    return ServiceSpec(stype, port, instance, bytes.fromhex(txt))


def list_adapters() -> list[Adapter]:
    """IPv4 adapters with a classification (real LAN vs virtual/VPN)."""
    try:
        import ifaddr
    except ImportError:
        return []
    out = []
    for ad in ifaddr.get_adapters():
        label = (ad.nice_name or ad.name or "").strip()
        low = label.lower()
        for ip in ad.ips:
            if not isinstance(ip.ip, str):
                continue
            addr = ipaddress.ip_address(ip.ip)
            if addr.is_loopback:
                continue
            reasons = []
            if addr.is_link_local:
                reasons.append("нет адреса от роутера (169.254.x.x)")
            hint = next((h for h in VIRTUAL_HINTS if h in low), None)
            if hint:
                reasons.append(f"виртуальный/VPN адаптер ({hint.strip()})")
            if not addr.is_private and not addr.is_link_local:
                reasons.append("не локальная сеть")
            out.append(Adapter(label, ip.ip, bool(reasons), ip.network_prefix, reasons))
    return out


def choose_addresses(adapters: list[Adapter], forced_ip: str = "") -> list[str]:
    if forced_ip:
        return [forced_ip]
    good = [a.ip for a in adapters if not a.virtual]
    if good:
        return good
    # Nothing looks like a LAN: fall back to every non-link-local address.
    return [a.ip for a in adapters if not a.ip.startswith("169.254.")]


def _host_label() -> str:
    raw = socket.gethostname() or "pc"
    label = re.sub(r"[^A-Za-z0-9-]", "-", raw).strip("-")[:40] or "pc"
    # Unique host name: Windows already answers for "<pc>.local" itself (with VPN IPs too).
    return f"iMirror-{label}-{uuid.getnode() & 0xFFFF:04x}"


class Advertiser:
    """Collects IMIRROR_MDNS lines and announces them with python-zeroconf."""

    def __init__(self, on_event):
        self.on_event = on_event          # on_event(kind, payload)
        self.specs: dict[str, ServiceSpec] = {}
        self._zc = None
        self._infos = []
        self._lock = threading.Lock()
        self.addresses: list[str] = []
        self.forced_ip = ""

    def feed(self, line: str) -> bool:
        spec = parse_mdns_line(line)
        if not spec:
            return False
        with self._lock:
            self.specs[spec.type] = spec
            ready = {"_airplay._tcp", "_raop._tcp"} <= set(self.specs)
        if ready:
            threading.Thread(target=self.start, daemon=True).start()
        return True

    def start(self):
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
        with self._lock:
            self._stop_locked()
            adapters = list_adapters()
            addrs = choose_addresses(adapters, self.forced_ip)
            if not addrs:
                self.on_event("mdns", {"ok": False, "error": "Нет подключения к сети"})
                return
            try:
                self._zc = Zeroconf(interfaces=addrs, ip_version=IPVersion.V4Only)
            except OSError as e:
                self.on_event("mdns", {"ok": False, "error": f"UDP 5353 недоступен: {e}"})
                return
            host = _host_label() + ".local."
            packed = [socket.inet_aton(a) for a in addrs]
            for spec in (self.specs["_airplay._tcp"], self.specs["_raop._tcp"]):
                stype = spec.type + ".local."
                info = ServiceInfo(stype, f"{spec.instance}.{stype}", port=spec.port,
                                   properties=spec.txt, server=host, addresses=packed)
                try:
                    self._zc.register_service(info, allow_name_change=True, cooperating_responders=False)
                    self._infos.append(info)
                except Exception as e:     # NonUniqueNameException, OSError ...
                    self.on_event("mdns", {"ok": False, "error": f"{spec.type}: {e}"})
                    return
            self.addresses = addrs
            self.on_event("mdns", {"ok": True, "addresses": addrs,
                                   "adapters": [a.__dict__ for a in adapters]})

    def _stop_locked(self):
        if self._zc is not None:
            try:
                for info in self._infos:
                    self._zc.unregister_service(info)
            except Exception:
                pass
            try:
                self._zc.close()
            except Exception:
                pass
        self._zc = None
        self._infos = []

    def stop(self):
        with self._lock:
            self._stop_locked()
            self.specs.clear()

    def restart(self):
        if {"_airplay._tcp", "_raop._tcp"} <= set(self.specs):
            threading.Thread(target=self.start, daemon=True).start()
