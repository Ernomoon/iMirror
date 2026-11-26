"""Network self-check: everything that can stop an iPhone from seeing or reaching the PC.

Each check returns {"id", "title", "state": ok|warn|bad|info, "detail", "fix"?}. Fixes that
need administrator rights are run elevated (Windows asks the user).
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from .mdns import Bonjour, bonjour_running, list_adapters, _powershell

log = logging.getLogger("imirror")
IS_WIN = sys.platform == "win32"
RULE_APP, RULE_RX, RULE_PORTS = "iMirror", "iMirror AirPlay", "iMirror AirPlay ports"
TCP_PORTS = "7000,7001,7100,7020,7021,7120,7040,7041,7140"
UDP_PORTS = "5353,6000,6001,7011,6020,6021,7031,6040,6041,7051"


def _run(cmd, timeout=15):
    flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
    return r.stdout.decode("cp866" if IS_WIN else "utf-8", "replace"), r.returncode


def run_elevated(ps_script: str) -> bool:
    """Run a PowerShell script as administrator (UAC prompt). Returns True if started."""
    if not IS_WIN:
        return False
    args = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "{ps_script}"'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", args, None, 0)
    return rc > 32


def firewall_rules() -> dict[str, bool]:
    """{DisplayName: enabled-and-allow} for iMirror's rules (locale-independent)."""
    out, _ = _powershell("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                         f"@(Get-NetFirewallRule -DisplayName '{RULE_APP}','{RULE_RX}','{RULE_PORTS}' "
                         "-ErrorAction SilentlyContinue | Select-Object DisplayName,"
                         "@{n='e';e={[string]$_.Enabled}},@{n='a';e={[string]$_.Action}},"
                         "@{n='d';e={[string]$_.Direction}}) | ConvertTo-Json -Compress", 20)
    try:
        data = json.loads(out.strip() or "[]")
    except ValueError:
        log.warning("firewall rules: %r", out[:300])
        return {}
    if isinstance(data, dict):
        data = [data]
    res: dict[str, bool] = {}
    for r in data:
        ok = r.get("e") == "True" and r.get("a") == "Allow" and r.get("d") == "Inbound"
        res[r.get("DisplayName", "")] = res.get(r.get("DisplayName", ""), False) or ok
    return res


def _third_party_firewalls():
    out, _ = _powershell("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                         "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName FirewallProduct "
                         "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty displayName | ConvertTo-Json -Compress")
    try:
        names = json.loads(out.strip() or "[]")
    except ValueError:
        return []
    if isinstance(names, str):
        names = [names]
    return [n for n in names if n and "windows" not in n.lower() and "microsoft" not in n.lower()]


def _port_listening(port: int) -> bool:
    """Is something LISTENING on this TCP port? (Never connect: UxPlay would count it as a client.)"""
    if not IS_WIN:
        return True
    out, _ = _run(["netstat", "-ano", "-p", "TCP"])
    return any(f":{port} " in line and ("LISTENING" in line or "ПРОСЛУШИВАНИЕ" in line) for line in out.splitlines())


def run_checks(app_exe: str, uxplay_exe: str, receiver_running: bool, port: int, mdns_state: dict) -> list[dict]:
    checks = []

    # 1. network adapters
    adapters = list_adapters(with_profiles=True)
    lan = [a for a in adapters if not a.virtual]
    vpn = [a for a in adapters if a.virtual and a.gateway]
    if lan:
        a = lan[0]
        kind = "Wi-Fi" if a.wifi else "Кабель"
        checks.append({"id": "lan", "title": "Подключение к сети", "state": "ok",
                       "detail": f"{kind}: {a.name}, адрес {a.ip}" + (f" и ещё {len(lan) - 1}" if len(lan) > 1 else "")})
    else:
        checks.append({"id": "lan", "title": "Подключение к сети", "state": "bad",
                       "detail": "Не найдено ни Wi-Fi, ни кабеля с адресом от роутера. Подключите компьютер к той же сети, что и iPhone."})

    # 2. VPN
    if vpn:
        names = ", ".join(sorted({a.description or a.name for a in vpn}))
        checks.append({"id": "vpn", "title": "VPN на компьютере", "state": "warn",
                       "detail": f"Активен: {names}. VPN с блокировкой локальной сети не пускает iPhone к компьютеру. "
                                 "Если iPhone не видит iMirror, выключите VPN или разрешите в нём локальную сеть."})
    else:
        checks.append({"id": "vpn", "title": "VPN на компьютере", "state": "ok", "detail": "Не мешает"})

    # 3. network profile
    public = [a for a in lan if a.profile.lower() == "public"]
    if public:
        idx = ",".join(str(a.index) for a in public)
        checks.append({"id": "profile", "title": "Тип сети Windows", "state": "bad",
                       "detail": f"Сеть «{public[0].name}» помечена как общедоступная: Windows прячет компьютер от других устройств.",
                       "fix": "profile", "fix_title": "Сделать частной", "fix_arg": idx})
    elif lan and any(a.profile for a in lan):
        checks.append({"id": "profile", "title": "Тип сети Windows", "state": "ok", "detail": "Частная сеть"})

    # 4. Windows firewall rules
    if IS_WIN:
        rules = firewall_rules()
        log.info("firewall rules: %s", rules)
        ok_rx = rules.get(RULE_RX, False)
        ok_ports = rules.get(RULE_PORTS, False)
        if ok_rx and ok_ports:
            checks.append({"id": "fw", "title": "Брандмауэр Windows", "state": "ok", "detail": "iMirror разрешён"})
        else:
            checks.append({"id": "fw", "title": "Брандмауэр Windows", "state": "bad",
                           "detail": "Нет разрешающего правила для iMirror: iPhone не сможет подключиться.",
                           "fix": "firewall", "fix_title": "Разрешить"})
        third = _third_party_firewalls()
        if third:
            checks.append({"id": "fw3", "title": "Сторонний брандмауэр", "state": "warn",
                           "detail": f"{', '.join(third)}. Разрешите в нём iMirror и uxplay.exe или доверяйте домашней сети."})

    # 5. Bonjour
    if IS_WIN:
        have = Bonjour.load() is not None
        running = bonjour_running() if have else False
        if have and running:
            checks.append({"id": "bonjour", "title": "Служба Bonjour", "state": "ok",
                           "detail": "Работает (запасной способ объявления)"})
        elif have:
            checks.append({"id": "bonjour", "title": "Служба Bonjour", "state": "bad",
                           "detail": "Установлена, но остановлена.", "fix": "bonjour_start", "fix_title": "Запустить"})
        else:
            from .netif import WinDnsSd
            if WinDnsSd.available():
                checks.append({"id": "bonjour", "title": "Служба объявления", "state": "ok",
                               "detail": "Встроенная в Windows (как в iPhoneMirror). Bonjour не нужен"})
            else:
                checks.append({"id": "bonjour", "title": "Служба Bonjour", "state": "info",
                               "detail": "Windows старше 10 1809: установите Bonjour, если iPhone не видит компьютер.",
                               "fix": "bonjour_install", "fix_title": "Установить"})

    # 6. receiver + announcement
    if receiver_running and port:
        listening = _port_listening(port)
        checks.append({"id": "port", "title": "Приёмник AirPlay", "state": "ok" if listening else "bad",
                       "detail": f"Принимает подключения на порту {port}" if listening else f"Порт {port} не отвечает"})
        if mdns_state.get("ok"):
            how = {"native": "встроенной службой Windows", "bonjour": "через Bonjour"}.get(
                mdns_state.get("method"), "собственным объявлением")
            checks.append({"id": "mdns", "title": "Виден в «Повторе экрана»", "state": "ok",
                           "detail": f"Объявлен {how}: " + ", ".join(mdns_state.get("addresses", []))})
        elif mdns_state.get("ok") is False:
            checks.append({"id": "mdns", "title": "Виден в «Повторе экрана»", "state": "bad",
                           "detail": mdns_state.get("error", "ошибка"), "fix": "reannounce", "fix_title": "Повторить"})
        else:
            checks.append({"id": "mdns", "title": "Виден в «Повторе экрана»", "state": "info", "detail": "Объявляю…"})
    else:
        checks.append({"id": "port", "title": "Приёмник AirPlay", "state": "info", "detail": "Выключен"})
    return checks


def apply_fix(fix: str, arg: str, app_exe: str, uxplay_exe: str) -> bool:
    log.info("fix %s %s", fix, arg)
    if fix == "firewall":
        script = (f"Remove-NetFirewallRule -DisplayName '{RULE_APP}','{RULE_RX}','{RULE_PORTS}' -ErrorAction SilentlyContinue; "
                  f"New-NetFirewallRule -DisplayName '{RULE_APP}' -Direction Inbound -Action Allow -Profile Any -Program '{app_exe}'; "
                  f"New-NetFirewallRule -DisplayName '{RULE_RX}' -Direction Inbound -Action Allow -Profile Any -Program '{uxplay_exe}'; "
                  f"New-NetFirewallRule -DisplayName '{RULE_PORTS}' -Direction Inbound -Action Allow -Profile Any -Protocol TCP -LocalPort {TCP_PORTS}; "
                  f"New-NetFirewallRule -DisplayName '{RULE_PORTS}' -Direction Inbound -Action Allow -Profile Any -Protocol UDP -LocalPort {UDP_PORTS}")
        return run_elevated(script.replace('"', "'"))
    if fix == "profile":
        idx = ",".join(i for i in arg.split(",") if i.isdigit())
        return run_elevated(f"Set-NetConnectionProfile -InterfaceIndex {idx} -NetworkCategory Private")
    if fix == "bonjour_start":
        return run_elevated("Set-Service -Name 'Bonjour Service' -StartupType Automatic; Start-Service -Name 'Bonjour Service'")
    if fix == "bonjour_install":
        url = "https://download.info.apple.com/Mac_OS_X/061-8098.20100603.gthyu/BonjourPSSetup.exe"
        tmp = Path(os.environ.get("TEMP", ".")) / "BonjourPSSetup.exe"
        return run_elevated(f"Invoke-WebRequest -UseBasicParsing '{url}' -OutFile '{tmp}'; "
                            f"Start-Process -Wait '{tmp}' -ArgumentList '/quiet','/norestart'")
    return False


# ------------------------------------------------------------------------------ Wi-Fi tuning
class WifiStreaming:
    """While mirroring: Wi-Fi media-streaming mode on, background scans off (fewer stutters)."""

    OPCODE_BACKGROUND_SCAN = 2
    OPCODE_MEDIA_STREAMING = 3

    def __init__(self):
        self.handle = None
        self.guids = []

    def enable(self, on: bool):
        if not IS_WIN:
            return
        try:
            w = ctypes.WinDLL("wlanapi.dll")
        except OSError:
            return
        try:
            if self.handle is None:
                ver = ctypes.c_uint32()
                h = ctypes.c_void_p()
                if w.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h)) != 0:
                    return
                self.handle = h

                class GUID(ctypes.Structure):
                    _fields_ = [("d", ctypes.c_byte * 16)]

                class INFO(ctypes.Structure):
                    _fields_ = [("guid", GUID), ("desc", ctypes.c_wchar * 256), ("state", ctypes.c_uint32)]

                class LIST(ctypes.Structure):
                    _fields_ = [("n", ctypes.c_uint32), ("i", ctypes.c_uint32), ("items", INFO * 16)]
                p = ctypes.POINTER(LIST)()
                if w.WlanEnumInterfaces(self.handle, None, ctypes.byref(p)) == 0 and p:
                    lst = p.contents
                    self.guids = [bytes(lst.items[k].guid.d) for k in range(min(lst.n, 16))]
                    w.WlanFreeMemory(p)
            for g in self.guids:
                gbuf = ctypes.create_string_buffer(g, 16)
                v_ms = ctypes.c_uint32(1 if on else 0)
                v_bs = ctypes.c_uint32(0 if on else 1)
                r1 = w.WlanSetInterface(self.handle, gbuf, self.OPCODE_MEDIA_STREAMING, 4, ctypes.byref(v_ms), None)
                r2 = w.WlanSetInterface(self.handle, gbuf, self.OPCODE_BACKGROUND_SCAN, 4, ctypes.byref(v_bs), None)
                log.info("wifi streaming=%s: media=%s scan=%s", on, r1, r2)
        except Exception as e:
            log.warning("wifi tuning: %r", e)
