"""Windows network adapters (GetAdaptersAddresses) and native DNS-SD (DnsServiceRegister).

Approach taken from iPhoneMirror (GPL-3.0, github.com/RayrenSX/iPhoneMirror), which works on
PCs where other receivers stay invisible: register the AirPlay services through Windows' own
mDNS responder (dnsapi.dll, Windows 10 1809+) on the best physical adapter, instead of
running a second responder that competes with Windows for UDP 5353.
"""
from __future__ import annotations

import ctypes
import logging
import socket
import sys
import threading
from ctypes import wintypes as W
from dataclasses import dataclass, field

log = logging.getLogger("imirror")
IS_WIN = sys.platform == "win32"

IF_TYPE_ETHERNET = 6
IF_TYPE_PPP = 23
IF_TYPE_LOOPBACK = 24
IF_TYPE_IEEE80211 = 71
IF_TYPE_TUNNEL = 131
IF_TYPE_WWANPP = 243
IF_TYPE_WWANPP2 = 244
IF_OPER_UP = 1
IP_ADAPTER_NO_MULTICAST = 0x0010
GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
AF_INET = 2

VIRTUAL_MARKERS = ("virtual", "veth", "vmware", "hyper-v", "hyperv", "virtualbox", "tap", "vpn", "loopback",
                   "tunnel", "wsl", "docker", "tailscale", "zerotier", "wireguard", "amnezia", "wintun",
                   "openvpn", "radmin", "hamachi", "nordlynx", "outline", "happ", "sing-box", "clash", "v2ray",
                   "npcap", "bluetooth", "teredo", "isatap", "fortinet", "anyconnect", "pangp")


@dataclass
class Iface:
    index: int
    name: str
    description: str
    if_type: int
    ips: list = field(default_factory=list)       # [(ip, prefix)]
    gateway: bool = False
    metric: int = 1 << 30
    multicast: bool = True

    @property
    def physical(self) -> bool:
        return self.if_type in (IF_TYPE_ETHERNET, IF_TYPE_IEEE80211, IF_TYPE_WWANPP, IF_TYPE_WWANPP2)

    @property
    def wifi(self) -> bool:
        return self.if_type == IF_TYPE_IEEE80211

    @property
    def looks_virtual(self) -> bool:
        low = (self.name + " " + self.description).lower()
        return any(m in low for m in VIRTUAL_MARKERS)

    @property
    def lan(self) -> bool:
        return self.physical and not self.looks_virtual and bool(self.ips)


# ------------------------------------------------------------------ GetAdaptersAddresses
class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", W.ULONG), ("Flags", W.DWORD), ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS), ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int), ("ValidLifetime", W.ULONG), ("PreferredLifetime", W.ULONG),
    ("LeaseLifetime", W.ULONG), ("OnLinkPrefixLength", ctypes.c_uint8)]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", W.ULONG), ("IfIndex", W.DWORD), ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p), ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", W.ULONG),
    ("Flags", W.ULONG), ("Mtu", W.ULONG), ("IfType", W.DWORD), ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", W.DWORD), ("ZoneIndices", W.ULONG * 16), ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
    ("FirstWinsServerAddress", ctypes.c_void_p), ("FirstGatewayAddress", ctypes.c_void_p),
    ("Ipv4Metric", W.ULONG), ("Ipv6Metric", W.ULONG)]


def interfaces() -> list[Iface]:
    if not IS_WIN:
        return []
    iphlp = ctypes.WinDLL("iphlpapi.dll")
    fn = iphlp.GetAdaptersAddresses
    fn.restype = W.ULONG
    fn.argtypes = [W.ULONG, W.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(W.ULONG)]
    size = W.ULONG(32 * 1024)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        rc = fn(AF_INET, GAA_FLAG_INCLUDE_GATEWAYS, None, buf, ctypes.byref(size))
        if rc == 0:
            break
        if rc != 111:          # ERROR_BUFFER_OVERFLOW
            raise OSError(f"GetAdaptersAddresses error {rc}")
    else:
        raise OSError("GetAdaptersAddresses: buffer")
    out = []
    p = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while p:
        a = p.contents
        if a.OperStatus == IF_OPER_UP and a.IfType not in (IF_TYPE_LOOPBACK,):
            ips = []
            u = a.FirstUnicastAddress
            while u:
                sa = u.contents.Address
                if sa.lpSockaddr and sa.iSockaddrLength >= 8:
                    raw = ctypes.string_at(sa.lpSockaddr, 8)
                    if int.from_bytes(raw[0:2], "little") == AF_INET:
                        ip = socket.inet_ntoa(raw[4:8])
                        if not ip.startswith("169.254."):
                            ips.append((ip, u.contents.OnLinkPrefixLength))
                u = u.contents.Next
            out.append(Iface(int(a.IfIndex), a.FriendlyName or "", a.Description or "", int(a.IfType), ips,
                             bool(a.FirstGatewayAddress), int(a.Ipv4Metric),
                             not (a.Flags & IP_ADAPTER_NO_MULTICAST)))
        p = a.Next
    return out


def best_interface(ifaces: list[Iface], requested_ip: str = "") -> Iface | None:
    """Same preference order as iPhoneMirror: requested > non-virtual > physical > gateway > metric."""
    cands = [i for i in ifaces if i.ips and i.multicast and i.if_type not in (IF_TYPE_TUNNEL, IF_TYPE_LOOPBACK)]
    if not cands:
        return None

    def key(i: Iface):
        requested = bool(requested_ip) and any(ip == requested_ip for ip, _ in i.ips)
        rank = 3 if i.if_type in (IF_TYPE_ETHERNET, IF_TYPE_IEEE80211) else 2 if i.physical else 1
        return (not requested, i.looks_virtual, not i.physical, not i.gateway, i.metric, -rank, i.index)
    return sorted(cands, key=key)[0]


# ------------------------------------------------------------------ native DNS-SD
class DNS_SERVICE_INSTANCE(ctypes.Structure):
    _fields_ = [("pszInstanceName", ctypes.c_wchar_p), ("pszHostName", ctypes.c_wchar_p),
                ("ip4Address", ctypes.c_void_p), ("ip6Address", ctypes.c_void_p),
                ("wPort", W.WORD), ("wPriority", W.WORD), ("wWeight", W.WORD),
                ("dwPropertyCount", W.DWORD), ("keys", ctypes.c_void_p), ("values", ctypes.c_void_p),
                ("dwInterfaceIndex", W.DWORD)]


REGISTER_COMPLETE = ctypes.WINFUNCTYPE(None, W.DWORD, ctypes.c_void_p, ctypes.POINTER(DNS_SERVICE_INSTANCE)) \
    if IS_WIN else ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)


class DNS_SERVICE_REGISTER_REQUEST(ctypes.Structure):
    _fields_ = [("Version", W.ULONG), ("InterfaceIndex", W.ULONG),
                ("pServiceInstance", ctypes.POINTER(DNS_SERVICE_INSTANCE)),
                ("pRegisterCompletionCallback", REGISTER_COMPLETE), ("pQueryContext", ctypes.c_void_p),
                ("hCredentials", W.HANDLE), ("unicastEnabled", W.BOOL)]


class DNS_SERVICE_CANCEL(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_void_p)]


DNS_REQUEST_PENDING = 9506


def parse_txt(txt: bytes) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(txt):
        n = txt[i]
        item = txt[i + 1:i + 1 + n].decode("utf-8", "replace")
        i += 1 + n
        if item:
            k, _, v = item.partition("=")
            out.append((k, v))
    return out


def host_name() -> str:
    if not IS_WIN:
        return socket.gethostname() + ".local"
    k32 = ctypes.WinDLL("kernel32")
    size = W.DWORD(0)
    k32.GetComputerNameExW(1, None, ctypes.byref(size))          # ComputerNameDnsHostname
    buf = ctypes.create_unicode_buffer(size.value + 1)
    if not k32.GetComputerNameExW(1, buf, ctypes.byref(size)):
        return "iMirror.local"
    return buf.value + ".local"


class WinDnsSd:
    """Registers services through Windows' built-in mDNS responder."""

    def __init__(self):
        self.dll = None
        self._regs = []         # (request, instance, keepalive)
        self._cb = None
        self._noop = REGISTER_COMPLETE(lambda *a: None)

    @staticmethod
    def available() -> bool:
        if not IS_WIN:
            return False
        try:
            return hasattr(ctypes.WinDLL("dnsapi.dll"), "DnsServiceRegister")
        except OSError:
            return False

    def _load(self):
        d = ctypes.WinDLL("dnsapi.dll")
        d.DnsServiceConstructInstance.restype = ctypes.POINTER(DNS_SERVICE_INSTANCE)
        d.DnsServiceConstructInstance.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
                                                  W.WORD, W.WORD, W.WORD, W.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        d.DnsServiceRegister.restype = W.DWORD
        d.DnsServiceRegister.argtypes = [ctypes.POINTER(DNS_SERVICE_REGISTER_REQUEST), ctypes.POINTER(DNS_SERVICE_CANCEL)]
        d.DnsServiceDeRegister.restype = W.DWORD
        d.DnsServiceDeRegister.argtypes = [ctypes.POINTER(DNS_SERVICE_REGISTER_REQUEST), ctypes.POINTER(DNS_SERVICE_CANCEL)]
        d.DnsServiceFreeInstance.restype = None
        d.DnsServiceFreeInstance.argtypes = [ctypes.POINTER(DNS_SERVICE_INSTANCE)]
        self.dll = d

    def register(self, specs, if_index: int, timeout=8.0):
        """specs: mdns.ServiceSpec list. Returns (ok, message)."""
        if self.dll is None:
            self._load()
        host = host_name()
        results = {}
        done = threading.Event()

        def complete(status, ctx, inst):
            results[int(ctx or 0)] = int(status)
            if len(results) >= len(specs):
                done.set()
            try:
                if inst:
                    self.dll.DnsServiceFreeInstance(inst)
            except Exception:
                pass
        self._cb = REGISTER_COMPLETE(complete)
        for n, spec in enumerate(specs, start=1):
            props = parse_txt(spec.txt)
            keys = (ctypes.c_wchar_p * max(1, len(props)))(*[k for k, _ in props])
            values = (ctypes.c_wchar_p * max(1, len(props)))(*[v for _, v in props])
            name = f"{spec.instance}.{spec.type}.local"
            inst = self.dll.DnsServiceConstructInstance(name, host, None, None, spec.port, 0, 0, len(props),
                                                        ctypes.cast(keys, ctypes.c_void_p),
                                                        ctypes.cast(values, ctypes.c_void_p))
            if not inst:
                return False, f"DnsServiceConstructInstance не удалось для {spec.type}"
            req = DNS_SERVICE_REGISTER_REQUEST()
            req.Version = 1
            req.InterfaceIndex = if_index
            req.pServiceInstance = inst
            req.pRegisterCompletionCallback = self._cb
            req.pQueryContext = ctypes.c_void_p(n)
            req.hCredentials = None
            req.unicastEnabled = False
            cancel = DNS_SERVICE_CANCEL()
            rc = self.dll.DnsServiceRegister(ctypes.byref(req), ctypes.byref(cancel))
            log.info("DnsServiceRegister %s on if %s -> %s", name, if_index, rc)
            self._regs.append((req, inst, cancel, keys, values))
            if rc != DNS_REQUEST_PENDING and rc != 0:
                self.stop()
                return False, f"Windows DNS-SD вернул ошибку {rc}"
        if not done.wait(timeout):
            return True, "Windows DNS-SD: регистрация отправлена (без подтверждения)"
        bad = {k: v for k, v in results.items() if v != 0}
        if bad:
            self.stop()
            return False, f"Windows DNS-SD: ошибка регистрации {bad}"
        return True, f"встроенным mDNS Windows (интерфейс {if_index})"

    def stop(self):
        if self.dll is None:
            return
        for req, inst, cancel, *_ in self._regs:
            try:
                req.pRegisterCompletionCallback = self._noop
                self.dll.DnsServiceDeRegister(ctypes.byref(req), None)
            except Exception as e:
                log.warning("DnsServiceDeRegister: %r", e)
        self._regs = []
