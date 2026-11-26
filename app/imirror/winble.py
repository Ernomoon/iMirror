"""Built-in Bluetooth of the PC as an iPhone keyboard + mouse (HID over GATT, no extra hardware).

Windows 10 1703+ can act as a Bluetooth LE peripheral (GATT server) through the WinRT
GattServiceProvider API, with the PC's own adapter, no driver and no admin rights. iMirror
publishes the standard HID service (0x1812) with the same composite report map as the
ESP32 firmware; the iPhone sees an ordinary Bluetooth keyboard/mouse, and with
AssistiveTouch on the mouse moves an on-screen pointer.

Same interface as bridge.SerialLink (write(frames), connected, phone_connected, start/stop),
so the Controller and the UI don't care which transport is used. Frames are executed by
hid_engine.HidEngine — the Python twin of the firmware scheduler.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid

from .hid_engine import HidEngine, REPORT_MAP, RID_KEYBOARD, RID_MOUSE, RID_CONSUMER


def _u16(short: int) -> uuid.UUID:
    return uuid.UUID(f"0000{short:04x}-0000-1000-8000-00805f9b34fb")


UUID_HID_SERVICE = _u16(0x1812)
UUID_REPORT = _u16(0x2A4D)
UUID_REPORT_MAP = _u16(0x2A4B)
UUID_HID_INFO = _u16(0x2A4A)
UUID_HID_CONTROL = _u16(0x2A4C)
UUID_PROTOCOL_MODE = _u16(0x2A4E)
UUID_REPORT_REFERENCE = _u16(0x2908)

REPORT_INPUT, REPORT_OUTPUT = 1, 2
# One report per BLE connection event: iOS uses 15 ms for HID; reports sent closer together
# arrive in the same event and the pointer motion becomes uneven.
INTERVAL_MS = 15

ERRORS_RU = {
    1: "Bluetooth выключен или адаптера нет (включите Bluetooth в параметрах Windows)",
    2: "Bluetooth-адаптер занят другим приложением",
    4: "внутренняя ошибка Bluetooth Windows",
    5: "Windows запретила публикацию HID-сервиса (политика). Используйте плату ESP32",
    6: "адаптер не поддерживает режим периферийного устройства (нужен Bluetooth 4.0+ с драйвером Microsoft)",
    7: "Bluetooth отключён пользователем",
    8: "нужно разрешение на Bluetooth (Параметры → Конфиденциальность → Bluetooth)",
    9: "адаптер не поддерживает Bluetooth LE",
}


def available() -> bool:
    try:
        import winrt.windows.devices.bluetooth.genericattributeprofile  # noqa: F401
        return True
    except Exception:
        return False


class WinBleLink:
    kind = "ble"

    def __init__(self, on_event):
        self.on_event = on_event
        self.engine = HidEngine(interval_ms=INTERVAL_MS)
        self.port_name = "Bluetooth компьютера"
        self.fw_info = ""
        self.phone_connected = False
        self._lock = threading.Lock()
        self._thread = None
        self._loop = None
        self._stop = threading.Event()
        self._provider = None
        self._chars = {}
        self._tokens = []
        self.error = ""

    # ------------------------------------------------------------- SerialLink-compatible API
    @property
    def connected(self) -> bool:
        return bool(self.fw_info)

    def start(self, _port_name: str = ""):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="iMirror-ble", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        was = self.connected
        self.fw_info = ""
        self.phone_connected = False
        if was:
            self.on_event("esp", "closed")

    def write(self, data: bytes) -> bool:
        if not self.connected:
            return False
        with self._lock:
            self.engine.feed(data, int(time.monotonic() * 1000))
        return True

    # ------------------------------------------------------------- thread
    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            self.error = f"{e!r}"
            self.on_event("esp", f"error:Bluetooth: {e}")
        finally:
            try:
                if self._provider is not None:
                    self._provider.stop_advertising()
            except Exception:
                pass
            self._provider = None
            loop.close()

    async def _main(self):
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattServiceProvider, GattLocalCharacteristicParameters, GattCharacteristicProperties as CP,
            GattProtectionLevel as PL, GattLocalDescriptorParameters,
            GattServiceProviderAdvertisingParameters)

        res = await GattServiceProvider.create_async(UUID_HID_SERVICE)
        if int(res.error) != 0:
            code = int(res.error)
            self.error = ERRORS_RU.get(code, f"код {code}")
            self.on_event("esp", f"error:Bluetooth: {self.error}")
            return
        provider = res.service_provider
        service = provider.service
        enc = PL.ENCRYPTION_REQUIRED

        async def char(uuid_, props, value=None, read=enc, write=enc):
            p = GattLocalCharacteristicParameters()
            p.characteristic_properties = props
            p.read_protection_level = read
            p.write_protection_level = write
            if value is not None:
                p.static_value = bytes(value)
            r = await service.create_characteristic_async(uuid_, p)
            if int(r.error) != 0:
                raise RuntimeError(f"characteristic {uuid_}: {ERRORS_RU.get(int(r.error), int(r.error))}")
            return r.characteristic

        async def report_ref(c, rid, rtype):
            d = GattLocalDescriptorParameters()
            d.read_protection_level = enc
            d.static_value = bytes([rid, rtype])
            r = await c.create_descriptor_async(UUID_REPORT_REFERENCE, d)
            if int(r.error) != 0:
                raise RuntimeError(f"report reference: {int(r.error)}")

        # Static characteristics (Windows answers reads itself).
        await char(UUID_HID_INFO, CP.READ, [0x11, 0x01, 0x00, 0x02])          # HID 1.11, normally connectable
        await char(UUID_REPORT_MAP, CP.READ, REPORT_MAP)
        ctrl = await char(UUID_HID_CONTROL, CP.WRITE_WITHOUT_RESPONSE)
        proto = await char(UUID_PROTOCOL_MODE, CP.READ | CP.WRITE_WITHOUT_RESPONSE, [0x01])

        notify_props = CP.READ | CP.NOTIFY
        kb = await char(UUID_REPORT, notify_props, bytes(8))
        await report_ref(kb, RID_KEYBOARD, REPORT_INPUT)
        mouse = await char(UUID_REPORT, notify_props, bytes(5))
        await report_ref(mouse, RID_MOUSE, REPORT_INPUT)
        cons = await char(UUID_REPORT, notify_props, bytes(2))
        await report_ref(cons, RID_CONSUMER, REPORT_INPUT)
        leds = await char(UUID_REPORT, CP.READ | CP.WRITE | CP.WRITE_WITHOUT_RESPONSE, bytes(1))
        await report_ref(leds, RID_KEYBOARD, REPORT_OUTPUT)
        self._chars = {RID_KEYBOARD: kb, RID_MOUSE: mouse, RID_CONSUMER: cons}

        def on_subscribers(sender, _args):
            self._update_phone()

        for c in (kb, mouse, cons):
            self._tokens.append((c, "remove_subscribed_clients_changed", c.add_subscribed_clients_changed(on_subscribers)))

        def on_write(sender, args):
            # Write-without-response writes (HID control point, LEDs, protocol mode) — just accept.
            deferral = args.get_deferral()

            async def finish():
                try:
                    req = await args.get_request_async()
                    if req is not None and int(req.option) == 0:
                        req.respond()
                finally:
                    deferral.complete()
            asyncio.run_coroutine_threadsafe(finish(), self._loop)

        for c in (ctrl, proto, leds):
            self._tokens.append((c, "remove_write_requested", c.add_write_requested(on_write)))

        adv = GattServiceProviderAdvertisingParameters()
        adv.is_connectable = True
        adv.is_discoverable = True
        provider.start_advertising(adv)
        self._provider = provider

        # Wait for advertising to start (or fail).
        for _ in range(50):
            st = int(provider.advertisement_status)
            if st in (2, 4):      # STARTED / STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA
                break
            if st == 3:           # ABORTED
                raise RuntimeError("Windows прервала Bluetooth-объявление (адаптер занят или не поддерживает режим)")
            await asyncio.sleep(0.1)

        self.fw_info = "Bluetooth компьютера · HID через GATT (без доп. устройств)"
        self.on_event("esp", "ready")
        self._update_phone()
        await self._pump(provider)

    def _update_phone(self):
        try:
            mouse = self._chars.get(RID_MOUSE)
            n = len(mouse.subscribed_clients) if mouse is not None else 0
        except Exception:
            n = 0
        on = n > 0
        if on != self.phone_connected:
            self.phone_connected = on
            with self._lock:
                self.engine.set_connected(on)
            self.on_event("phone", on)

    async def _pump(self, provider):
        last_status_check = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_status_check > 1.0:
                last_status_check = now
                self._update_phone()
                if int(provider.advertisement_status) in (1, 3) and not self.phone_connected:
                    # Stopped/aborted (e.g. adapter reset): try to restart advertising.
                    try:
                        from winrt.windows.devices.bluetooth.genericattributeprofile import (
                            GattServiceProviderAdvertisingParameters)
                        adv = GattServiceProviderAdvertisingParameters()
                        adv.is_connectable = True
                        adv.is_discoverable = True
                        provider.start_advertising(adv)
                    except Exception:
                        pass
            with self._lock:
                reports = self.engine.tick(int(now * 1_000_000))
            for rid, data in reports:
                c = self._chars.get(rid)
                if c is None:
                    continue
                try:
                    await c.notify_value_async(bytes(data))
                except Exception as e:
                    self.on_event("log", f"Bluetooth: ошибка отправки: {e}")
            await asyncio.sleep(0.002)
        for c, remover, tok in self._tokens:
            try:
                getattr(c, remover)(tok)
            except Exception:
                pass
        self._tokens.clear()
