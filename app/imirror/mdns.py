"""AirPlay service announcement (mDNS / Bonjour) done by iMirror itself.

UxPlay (patched) only prints its service data (IMIRROR_MDNS lines). iMirror announces it with
the most reliable method available on this PC:

  1. Apple Bonjour service (dnssd.dll, mDNSResponder) — the system responder that owns UDP
     5353 and answers every network with that network's own address (LonelyScreen does this).
  2. python-zeroconf on the real LAN adapters — when Bonjour is not installed. Runs with a
     watchdog so it can never hang silently.
"""
from __future__ import annotations

import ctypes
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict

log = logging.getLogger("imirror")

_LINE = re.compile(r"IMIRROR_MDNS type=(_\w+\._tcp) port=(\d+) name=(.+?) txt=([0-9a-fA-F]*)\s*$")

VIRTUAL_HINTS = (
    "hyper-v", "vethernet", "wsl", "virtualbox", "vmware", "vmnet", "virtual", "loopback",
    "tap-", "tap ", "tun", "wintun", "wireguard", "amnezia", "openvpn", "tailscale", "zerotier", "hamachi",
    "radmin", "nordlynx", "protonvpn", "expressvpn", "outline", "v2ray", "clash", "happ", "sing-box",
    "docker", "npcap", "bluetooth", "teredo", "isatap", "vpn", "fortinet", "cisco anyconnect", "pangp",
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
    description: str = ""
    index: int = 0
    gateway: bool = False
    wifi: bool = False
    profile: str = ""          # Public / Private / DomainAuthenticated


def parse_mdns_line(line: str) -> ServiceSpec | None:
    m = _LINE.search(line)
    if not m:
        return None
    stype, port, full, txt = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    suffix = f".{stype}.local"
    instance = full[:-len(suffix)] if full.endswith(suffix) else full.split(".")[0]
    return ServiceSpec(stype, port, instance, bytes.fromhex(txt))


# ---------------------------------------------------------------------------------- adapters
_PS_ADAPTERS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($a in Get-NetAdapter | Where-Object Status -eq 'Up') {
  $ips = @(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 | ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)" })
  $gw = [bool](Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix '0.0.0.0/0')
  $prof = (Get-NetConnectionProfile -InterfaceIndex $a.ifIndex).NetworkCategory
  $out += [pscustomobject]@{ name=$a.Name; desc=$a.InterfaceDescription; idx=$a.ifIndex; hw=[bool]$a.HardwareInterface;
    virt=[bool]$a.Virtual; media=[string]$a.NdisPhysicalMedium; ips=$ips; gw=$gw; profile=[string]$prof }
}
ConvertTo-Json -InputObject @($out) -Depth 3 -Compress
"""


def _powershell(script: str, timeout=25):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True, timeout=timeout, creationflags=flags)
    return r.stdout.decode("utf-8", "replace"), r.returncode


def _adapters_windows() -> list[Adapter]:
    out, _ = _powershell("[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + _PS_ADAPTERS)
    data = json.loads(out.strip() or "[]")
    if isinstance(data, dict):
        data = [data]
    res = []
    for a in data:
        desc = a.get("desc") or ""
        name = a.get("name") or desc
        low = (name + " " + desc).lower()
        media = (a.get("media") or "").lower()
        wifi = "802.11" in media or "native 802.11" in media or "wi-fi" in low or "wireless" in low or "wlan" in low
        for ipp in a.get("ips") or []:
            ip, _, pfx = ipp.partition("/")
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.is_loopback:
                continue
            reasons = []
            if addr.is_link_local:
                reasons.append("нет адреса от роутера (169.254.x.x)")
            hint = next((h for h in VIRTUAL_HINTS if h in low), None)
            if hint or not a.get("hw") or a.get("virt"):
                reasons.append(f"виртуальный адаптер или VPN{f' ({hint.strip()})' if hint else ''}")
            res.append(Adapter(name, ip, bool(reasons), int(pfx or 24), reasons, desc, int(a.get("idx") or 0),
                               bool(a.get("gw")), wifi, a.get("profile") or ""))
    return res


def _adapters_ifaddr() -> list[Adapter]:
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
                reasons.append(f"виртуальный адаптер или VPN ({hint.strip()})")
            if not addr.is_private and not addr.is_link_local:
                reasons.append("не локальная сеть")
            out.append(Adapter(label, ip.ip, bool(reasons), ip.network_prefix, reasons, label,
                               wifi=("wi-fi" in low or "wlan" in low or "wireless" in low)))
    return out


def list_adapters() -> list[Adapter]:
    """IPv4 adapters with a classification (real LAN vs virtual/VPN)."""
    if sys.platform == "win32":
        try:
            res = _adapters_windows()
            if res:
                return res
        except Exception as e:
            log.warning("adapter scan via PowerShell failed: %r", e)
    return _adapters_ifaddr()


def choose_addresses(adapters: list[Adapter], forced_ip: str = "") -> list[str]:
    if forced_ip:
        return [forced_ip]
    good = [a.ip for a in adapters if not a.virtual]
    if good:
        return good
    return [a.ip for a in adapters if not a.ip.startswith("169.254.")]


def _host_label() -> str:
    raw = socket.gethostname() or "pc"
    label = re.sub(r"[^A-Za-z0-9-]", "-", raw).strip("-")[:40] or "pc"
    return f"iMirror-{label}-{uuid.getnode() & 0xFFFF:04x}"


# ---------------------------------------------------------------------------------- Bonjour
class Bonjour:
    """Registration through Apple's Bonjour service (dnssd.dll)."""

    def __init__(self):
        self.dll = None
        self.refs = []
        self._cbs = []
        self.results = {}

    @staticmethod
    def load():
        if sys.platform != "win32":
            return None
        try:
            dll = ctypes.WinDLL("dnssd.dll")
        except OSError:
            return None
        u16, u32, vp, cp = ctypes.c_uint16, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char_p
        dll.DNSServiceRegister.restype = ctypes.c_int32
        dll.DNSServiceRegister.argtypes = [ctypes.POINTER(vp), u32, u32, cp, cp, cp, cp, u16, u16, vp, vp, vp]
        dll.DNSServiceRefDeallocate.restype = None
        dll.DNSServiceRefDeallocate.argtypes = [vp]
        dll.DNSServiceProcessResult.restype = ctypes.c_int32
        dll.DNSServiceProcessResult.argtypes = [vp]
        dll.DNSServiceRefSockFD.restype = ctypes.c_int
        dll.DNSServiceRefSockFD.argtypes = [vp]
        return dll

    def register(self, specs, timeout=6.0):
        """Returns (ok, message)."""
        import select
        self.dll = self.load()
        if self.dll is None:
            return False, "Bonjour не установлен"
        CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32,
                                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p)
        for spec in specs:
            def cb(ref, flags, err, name, regtype, domain, ctx, _t=spec.type):
                self.results[_t] = (int(err), (name or b"").decode("utf-8", "replace"))
            c = CB(cb)
            self._cbs.append(c)
            ref = ctypes.c_void_p()
            txt = ctypes.create_string_buffer(spec.txt, len(spec.txt))
            err = self.dll.DNSServiceRegister(ctypes.byref(ref), 0, 0, spec.instance.encode("utf-8"),
                                              spec.type.encode(), None, None, socket.htons(spec.port),
                                              len(spec.txt), ctypes.cast(txt, ctypes.c_void_p), c, None)
            if err != 0:
                self.stop()
                if err == -65563:
                    return False, "служба Bonjour не запущена"
                return False, f"Bonjour вернул ошибку {err}"
            self.refs.append(ref)
        # wait for both registration replies
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.results) < len(specs):
            for ref in self.refs:
                fd = self.dll.DNSServiceRefSockFD(ref)
                try:
                    r, _, _ = select.select([fd], [], [], 0.2)
                except (OSError, ValueError):
                    r = [fd]
                    time.sleep(0.2)
                if r:
                    self.dll.DNSServiceProcessResult(ref)
        bad = {t: v for t, v in self.results.items() if v[0] != 0}
        if len(self.results) < len(specs):
            return False, "служба Bonjour не ответила"
        if bad:
            return False, f"Bonjour: ошибка регистрации {bad}"
        names = ", ".join(v[1] for v in self.results.values())
        return True, f"зарегистрировано в Bonjour: {names}"

    def stop(self):
        if self.dll is not None:
            for ref in self.refs:
                try:
                    self.dll.DNSServiceRefDeallocate(ref)
                except Exception:
                    pass
        self.refs = []
        self.results = {}


def bonjour_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["sc.exe", "query", "Bonjour Service"], capture_output=True, timeout=8,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout.decode("cp866", "replace")
        return "RUNNING" in out
    except Exception:
        return False


# ---------------------------------------------------------------------------------- advertiser
class Advertiser:
    """Collects IMIRROR_MDNS lines and announces them (Bonjour first, zeroconf fallback)."""

    def __init__(self, on_event):
        self.on_event = on_event          # on_event("mdns", dict)
        self.specs: dict[str, ServiceSpec] = {}
        self._zc = None
        self._infos = []
        self._bonjour = None
        self._lock = threading.Lock()
        self.addresses: list[str] = []
        self.forced_ip = ""
        self.method = ""
        self.port = 0

    def feed(self, line: str) -> bool:
        spec = parse_mdns_line(line)
        if not spec:
            return False
        log.info("service data: %s port %s", spec.type, spec.port)
        with self._lock:
            self.specs[spec.type] = spec
            ready = {"_airplay._tcp", "_raop._tcp"} <= set(self.specs)
        if ready:
            self.port = self.specs["_airplay._tcp"].port
            self.restart()
        return True

    def restart(self):
        if {"_airplay._tcp", "_raop._tcp"} <= set(self.specs):
            threading.Thread(target=self._start_guarded, name="iMirror-mdns", daemon=True).start()

    def _start_guarded(self):
        done = threading.Event()
        result = {}

        def run():
            try:
                result["r"] = self._start()
            except Exception as e:
                log.exception("announce failed")
                result["r"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            finally:
                done.set()
        t = threading.Thread(target=run, name="iMirror-mdns-worker", daemon=True)
        t.start()
        if not done.wait(25):
            import faulthandler
            import io
            buf = io.StringIO()
            try:
                faulthandler.dump_traceback(file=buf, all_threads=True)
            except Exception:
                pass
            log.error("announce hung >25 s; stacks:\n%s", buf.getvalue())
            self.on_event("mdns", {"ok": False, "error": "объявление в сети зависло (подробности в журнале)"})
            return
        self.on_event("mdns", result["r"])

    def _start(self):
        with self._lock:
            self._stop_locked()
            specs = [self.specs["_airplay._tcp"], self.specs["_raop._tcp"]]
            adapters = list_adapters()
            addrs = choose_addresses(adapters, self.forced_ip)
            log.info("adapters: %s", [(a.name, a.ip, a.virtual, a.gateway, a.profile) for a in adapters])
            ad = [asdict(a) for a in adapters]

            # 1) Apple Bonjour service
            method = os.environ.get("IMIRROR_MDNS_METHOD", "")      # tests: force "zeroconf"
            if not self.forced_ip and method != "zeroconf" and Bonjour.load() is not None:
                b = Bonjour()
                ok, msg = b.register(specs)
                log.info("bonjour: %s %s", ok, msg)
                if ok:
                    self._bonjour = b
                    self.method = "bonjour"
                    self.addresses = addrs
                    return {"ok": True, "method": "bonjour", "addresses": addrs, "adapters": ad, "detail": msg}
                b.stop()

            # 2) python-zeroconf on the LAN adapters
            if not addrs:
                return {"ok": False, "error": "компьютер не подключён к сети", "adapters": ad}
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
            log.info("zeroconf on %s", addrs)
            self._zc = Zeroconf(interfaces=addrs, ip_version=IPVersion.V4Only)
            host = _host_label() + ".local."
            packed = [socket.inet_aton(a) for a in addrs]
            for spec in specs:
                stype = spec.type + ".local."
                info = ServiceInfo(stype, f"{spec.instance}.{stype}", port=spec.port,
                                   properties=spec.txt, server=host, addresses=packed)
                self._zc.register_service(info, allow_name_change=True, cooperating_responders=True)
                self._infos.append(info)
                log.info("zeroconf registered %s", info.name)
            self.method = "zeroconf"
            self.addresses = addrs
            return {"ok": True, "method": "zeroconf", "addresses": addrs, "adapters": ad}

    def _stop_locked(self):
        if self._bonjour is not None:
            self._bonjour.stop()
            self._bonjour = None
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
        self.method = ""

    def stop(self):
        with self._lock:
            self._stop_locked()
            self.specs.clear()
