"""The PC's own Bluetooth as an iPhone keyboard + mouse (HID over GATT, no extra hardware).

Windows 10 1703+ can be a Bluetooth LE peripheral through the WinRT GattServiceProvider API.
iMirror publishes the standard HID service 0x1812; the iPhone pairs with it like with any
Bluetooth keyboard/mouse (Settings > Bluetooth). With AssistiveTouch on, the mouse moves an
on-screen pointer; clicks tap, the wheel scrolls, keys type.

GATT layout and report map follow iPhoneMirror (GPL-3.0, github.com/RayrenSX/iPhoneMirror)
which works with iOS on ordinary PC adapters:
  HID Information [0x11,0x01,0x00,0x03]  Protocol Mode Read|Write  Control Point WriteNoResp
  Report 1 keyboard (8 B)  Report 2 mouse (buttons, X16, Y16, wheel)  Report 3 feature (wheel
  resolution)  Report 4 consumer (16-bit usage)  Report 5 navigation (Back/Menu/Home/...)
  Boot Keyboard Input + Boot Mouse Input; all readable values need an encrypted link (pairing).
Mouse reports go out on an 8 ms cadence, one at a time; motion that piles up while a
notification is in flight is merged and capped, so the pointer never "replays" old movement.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid

log = logging.getLogger("imirror")


def _u16(short: int) -> uuid.UUID:
    return uuid.UUID(f"0000{short:04x}-0000-1000-8000-00805f9b34fb")


HID_SERVICE = _u16(0x1812)
REPORT = _u16(0x2A4D)
REPORT_MAP_UUID = _u16(0x2A4B)
HID_INFO = _u16(0x2A4A)
HID_CONTROL = _u16(0x2A4C)
PROTOCOL_MODE = _u16(0x2A4E)
BOOT_KB_IN = _u16(0x2A22)
BOOT_MOUSE_IN = _u16(0x2A33)
REPORT_REFERENCE = _u16(0x2908)

HID_INFORMATION = bytes([0x11, 0x01, 0x00, 0x03])
REPORT_MAP = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x75, 0x08, 0x95, 0x01,
    0x81, 0x01, 0x75, 0x08, 0x95, 0x06, 0x15, 0x00, 0x25, 0x65,
    0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00, 0xC0,
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x02, 0x09, 0x01,
    0xA1, 0x00, 0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00,
    0x25, 0x01, 0x75, 0x01, 0x95, 0x05, 0x81, 0x02, 0x75, 0x03,
    0x95, 0x01, 0x81, 0x01, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02,
    0x81, 0x06, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
    0x95, 0x01, 0x81, 0x06,
    0x85, 0x03,
    0x09, 0x48, 0x15, 0x00, 0x25, 0x0A, 0x75, 0x08, 0x95, 0x01,
    0xB1, 0x02, 0xC0, 0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x04,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01,
    0x19, 0x00, 0x2A, 0xFF, 0x03, 0x81, 0x00, 0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x05,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x0D,
    0x0A, 0x24, 0x02, 0x09, 0x40, 0x0A, 0x23, 0x02,
    0x0A, 0xAE, 0x01, 0x0A, 0x21, 0x02, 0x81, 0x02,
    0x95, 0x03, 0x75, 0x01, 0x81, 0x03, 0xC0,
])
RID_KB, RID_MOUSE, RID_FEATURE, RID_CONSUMER, RID_NAV = 1, 2, 3, 4, 5
NAV_BACK, NAV_MENU, NAV_HOME, NAV_LAYOUT, NAV_SEARCH = 0x01, 0x02, 0x04, 0x08, 0x10
CONSUMER = {"vol_up": 0x00E9, "vol_down": 0x00EA, "mute": 0x00E2, "play": 0x00CD,
            "next": 0x00B5, "prev": 0x00B6, "lock": 0x0030, "globe": 0x029D}

MOUSE_INTERVAL = 0.008
MOTION_CAP = 1200          # counts per report; more than this is stale and dropped
NOTIFY_SLOW = 0.3

ERRORS_RU = {
    1: "Bluetooth выключен (включите его в параметрах Windows)",
    2: "Bluetooth-адаптер занят другим приложением",
    4: "внутренняя ошибка Bluetooth Windows",
    5: "Windows запретила публикацию сервиса (политика Bluetooth)",
    6: "адаптер не поддерживает режим периферийного устройства",
    7: "Bluetooth отключён пользователем",
    8: "нет разрешения на Bluetooth (Параметры → Конфиденциальность → Bluetooth)",
    9: "адаптер не поддерживает Bluetooth LE",
}


def available() -> bool:
    try:
        import winrt.windows.devices.bluetooth.genericattributeprofile  # noqa: F401
        import winrt.windows.storage.streams  # noqa: F401
        return True
    except Exception:
        return False


def make_buffer(data: bytes):
    """bytes -> Windows IBuffer."""
    from winrt.windows.storage.streams import DataWriter
    w = DataWriter()
    w.write_bytes(bytes(data))
    return w.detach_buffer()


def mouse_report(buttons: int, dx: int, dy: int, wheel: int) -> bytes:
    dx = max(-32767, min(32767, int(dx)))
    dy = max(-32767, min(32767, int(dy)))
    wheel = max(-127, min(127, int(wheel)))
    return bytes([buttons & 0x1F, dx & 0xFF, (dx >> 8) & 0xFF, dy & 0xFF, (dy >> 8) & 0xFF, wheel & 0xFF])


def boot_mouse(report: bytes) -> bytes:
    dx = int.from_bytes(report[1:3], "little", signed=True)
    dy = int.from_bytes(report[3:5], "little", signed=True)
    c = lambda v: max(-127, min(127, v)) & 0xFF  # noqa: E731
    return bytes([report[0] & 0x07, c(dx), c(dy)])


class BleHid:
    """status: off / starting / waiting (advertising, iPhone not paired yet) / connected / error"""

    def __init__(self, on_change):
        self.on_change = on_change
        self.status = "off"
        self.error = ""
        self.adapter = ""
        self._thread = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._provider = None
        self._chars = {}
        self._tokens = []
        self._lock = threading.Lock()
        self._dx = self._dy = self._wheel = 0
        self._buttons = 0
        self._sent_buttons = 0
        self._pending = []          # (rid, bytes) non-mouse reports, in order
        self._wake = None
        self.subscribers = 0

    # ------------------------------------------------------------------ public (any thread)
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._set("starting")
        self._thread = threading.Thread(target=self._run, name="iMirror-bt", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._kick()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=4)
        self._thread = None
        self._set("off")

    @property
    def connected(self) -> bool:
        return self.status == "connected"

    def move(self, dx: float, dy: float):
        with self._lock:
            self._dx = max(-MOTION_CAP, min(MOTION_CAP, self._dx + dx))
            self._dy = max(-MOTION_CAP, min(MOTION_CAP, self._dy + dy))
        self._kick()

    def buttons(self, b: int):
        with self._lock:
            self._buttons = b & 0x1F
        self._kick()

    def scroll(self, steps: float):
        with self._lock:
            self._wheel = max(-127, min(127, self._wheel + steps))
        self._kick()

    def keys(self, mods: int, pressed: list[int]):
        rep = bytes([mods & 0xFF, 0] + (list(pressed)[:6] + [0] * 6)[:6])
        self._queue(RID_KB, rep)

    def consumer_tap(self, usage: int):
        self._queue(RID_CONSUMER, usage.to_bytes(2, "little"))
        self._queue(RID_CONSUMER, b"\x00\x00")

    def nav_tap(self, bits: int, double=False):
        self._queue(RID_NAV, bits.to_bytes(2, "little"))
        self._queue(RID_NAV, b"\x00\x00")
        if double:
            self._queue("sleep", 0.1)
            self._queue(RID_NAV, bits.to_bytes(2, "little"))
            self._queue(RID_NAV, b"\x00\x00")

    def release_all(self):
        with self._lock:
            self._dx = self._dy = self._wheel = 0
            self._buttons = 0
        self._queue(RID_KB, bytes(8))

    # ------------------------------------------------------------------ internals
    def _set(self, status, error=""):
        if status != self.status or error != self.error:
            self.status, self.error = status, error
            log.info("bluetooth: %s %s", status, error)
            try:
                self.on_change()
            except Exception:
                pass

    def _queue(self, rid, data):
        with self._lock:
            if len(self._pending) < 256:
                self._pending.append((rid, data))
        self._kick()

    def _kick(self):
        loop, ev = self._loop, self._wake
        if loop is not None and ev is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            log.exception("bluetooth failed")
            self._set("error", f"{type(e).__name__}: {e}")
        finally:
            try:
                if self._provider is not None:
                    self._provider.stop_advertising()
            except Exception:
                pass
            for c, remover, tok in self._tokens:
                try:
                    getattr(c, remover)(tok)
                except Exception:
                    pass
            self._tokens.clear()
            self._provider = None
            self._chars = {}
            self._loop = None
            loop.close()
            if self.status not in ("error",):
                self._set("off")

    async def _main(self):
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattServiceProvider, GattLocalCharacteristicParameters, GattCharacteristicProperties as CP,
            GattProtectionLevel as PL, GattLocalDescriptorParameters,
            GattServiceProviderAdvertisingParameters, GattWriteOption)

        self._wake = asyncio.Event()
        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            self._set("error", "на компьютере нет Bluetooth или он выключен")
            return
        if not adapter.is_low_energy_supported:
            self._set("error", ERRORS_RU[9])
            return
        self.adapter = "поддерживает режим периферии" if adapter.is_peripheral_role_supported else \
            "драйвер не сообщает о режиме периферии, пробую"
        log.info("bluetooth adapter: peripheral=%s", adapter.is_peripheral_role_supported)

        res = await GattServiceProvider.create_async(HID_SERVICE)
        if int(res.error) != 0 or res.service_provider is None:
            code = int(res.error)
            self._set("error", ERRORS_RU.get(code, f"ошибка Bluetooth {code}"))
            return
        provider = res.service_provider
        service = provider.service
        enc = PL.ENCRYPTION_REQUIRED

        async def char(uid, props, value: bytes, protect=True):
            p = GattLocalCharacteristicParameters()
            p.characteristic_properties = props
            readable = bool(int(props) & int(CP.READ))
            writable = bool(int(props) & (int(CP.WRITE) | int(CP.WRITE_WITHOUT_RESPONSE)))
            p.read_protection_level = enc if (readable and protect) else PL.PLAIN
            p.write_protection_level = enc if (writable and protect) else PL.PLAIN
            if readable:
                p.static_value = make_buffer(value)
            r = await service.create_characteristic_async(uid, p)
            if int(r.error) != 0 or r.characteristic is None:
                raise RuntimeError(f"характеристика {uid}: {ERRORS_RU.get(int(r.error), int(r.error))}")
            return r.characteristic

        async def report_ref(c, rid, rtype):
            d = GattLocalDescriptorParameters()
            d.read_protection_level = enc
            d.write_protection_level = enc
            d.static_value = make_buffer(bytes([rid, rtype]))
            r = await c.create_descriptor_async(REPORT_REFERENCE, d)
            if int(r.error) != 0:
                raise RuntimeError(f"report reference: {int(r.error)}")

        def on_write(sender, args):
            deferral = args.get_deferral()

            async def finish():
                try:
                    req = await args.get_request_async()
                    if req is not None and req.option == GattWriteOption.WRITE_WITH_RESPONSE:
                        req.respond()
                except Exception as e:
                    log.warning("bluetooth write: %r", e)
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(finish(), self._loop)

        proto = await char(PROTOCOL_MODE, CP.READ | CP.WRITE, b"\x01")
        feature = await char(REPORT, CP.READ | CP.WRITE, b"\x00")
        await report_ref(feature, RID_FEATURE, 3)
        await char(HID_INFO, CP.READ, HID_INFORMATION)
        await char(REPORT_MAP_UUID, CP.READ, REPORT_MAP)
        ctrl = await char(HID_CONTROL, CP.WRITE_WITHOUT_RESPONSE, b"\x00")
        notify = CP.READ | CP.NOTIFY
        kb = await char(REPORT, notify, bytes(8))
        await report_ref(kb, RID_KB, 1)
        mouse = await char(REPORT, notify, bytes(6))
        await report_ref(mouse, RID_MOUSE, 1)
        cons = await char(REPORT, notify, bytes(2))
        await report_ref(cons, RID_CONSUMER, 1)
        nav = await char(REPORT, notify, bytes(2))
        await report_ref(nav, RID_NAV, 1)
        boot_kb = await char(BOOT_KB_IN, notify, bytes(8))
        boot_ms = await char(BOOT_MOUSE_IN, notify, bytes(3))
        self._chars = {RID_KB: kb, RID_MOUSE: mouse, RID_CONSUMER: cons, RID_NAV: nav,
                       "boot_kb": boot_kb, "boot_mouse": boot_ms}

        for c in (proto, feature, ctrl):
            self._tokens.append((c, "remove_write_requested", c.add_write_requested(on_write)))

        def on_subs(sender, _args):
            self._kick()
        for c in (kb, mouse, cons, nav, boot_kb, boot_ms):
            self._tokens.append((c, "remove_subscribed_clients_changed", c.add_subscribed_clients_changed(on_subs)))

        def advertise():
            adv = GattServiceProviderAdvertisingParameters()
            adv.is_connectable = True
            adv.is_discoverable = True
            provider.start_advertising(adv)
        advertise()
        self._provider = provider
        started = False
        for _ in range(60):
            st = int(provider.advertisement_status)
            if st in (2, 4):
                started = True
                break
            if st == 3:
                break
            await asyncio.sleep(0.1)
        if not started:
            self._set("error", "Windows не дала включить Bluetooth-объявление "
                               "(адаптер занят или не умеет режим периферии)")
            return
        self._set("waiting")
        await self._pump(provider, advertise)

    def _subs(self) -> int:
        n = 0
        for key in (RID_MOUSE, RID_KB, "boot_mouse"):
            c = self._chars.get(key)
            try:
                n = max(n, len(c.subscribed_clients)) if c is not None else n
            except Exception:
                pass
        return n

    def _has_subs(self, key) -> bool:
        c = self._chars.get(key)
        try:
            return c is not None and len(c.subscribed_clients) > 0
        except Exception:
            return False

    async def _notify(self, key, data: bytes) -> bool:
        c = self._chars.get(key)
        if c is None:
            return False
        if key in ("boot_kb", "boot_mouse"):
            # boot protocol only for hosts that did not subscribe to the report characteristics
            if not self._has_subs(key) or self._has_subs(RID_KB if key == "boot_kb" else RID_MOUSE):
                return False
        try:
            t0 = time.monotonic()
            await c.notify_value_async(make_buffer(data))
            if time.monotonic() - t0 > NOTIFY_SLOW:
                log.info("bluetooth: slow notification %.0f ms", (time.monotonic() - t0) * 1000)
            return True
        except Exception as e:
            log.warning("bluetooth notify: %r", e)
            return False

    async def _pump(self, provider, advertise):
        last_check = 0.0
        last_mouse = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_check > 0.5:
                last_check = now
                n = self._subs()
                if n != self.subscribers:
                    self.subscribers = n
                    if n == 0:
                        self.release_all()
                self._set("connected" if n else "waiting")
                if int(provider.advertisement_status) in (1, 3):
                    try:
                        advertise()
                    except Exception as e:
                        log.warning("bluetooth re-advertise: %r", e)
            with self._lock:
                pending, self._pending = self._pending, []
            for rid, data in pending:
                if rid == "sleep":
                    await asyncio.sleep(data)
                    continue
                if self.subscribers:
                    await self._notify(rid, data)
                    if rid == RID_KB:
                        await self._notify("boot_kb", data)
            wait = MOUSE_INTERVAL - (time.monotonic() - last_mouse)
            if wait <= 0:
                with self._lock:
                    dx, dy, wh, b = int(self._dx), int(self._dy), int(self._wheel), self._buttons
                    self._dx -= dx
                    self._dy -= dy
                    self._wheel -= wh
                if (dx or dy or wh or b != self._sent_buttons) and self.subscribers:
                    rep = mouse_report(b, dx, dy, wh)
                    last_mouse = time.monotonic()
                    ok = await self._notify(RID_MOUSE, rep)
                    await self._notify("boot_mouse", boot_mouse(rep))
                    if ok:
                        self._sent_buttons = b
                    with self._lock:      # motion that arrived during a slow send: keep it capped
                        self._dx = max(-MOTION_CAP, min(MOTION_CAP, self._dx))
                        self._dy = max(-MOTION_CAP, min(MOTION_CAP, self._dy))
                    continue
                wait = 0.25
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.001, wait))
            except asyncio.TimeoutError:
                pass
