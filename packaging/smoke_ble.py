"""CI check on Windows: the WinRT Bluetooth (HID over GATT) code imports, builds IBuffers and
GATT parameters, and runs up to the adapter. GitHub runners have no Bluetooth radio, so "no
adapter" is the expected end — but API misuse (TypeError/AttributeError) must not happen."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from imirror import blehid  # noqa: E402

print("winrt available:", blehid.available())
if not blehid.available():
    sys.exit("winrt packages missing")

from winrt.windows.devices.bluetooth.genericattributeprofile import (  # noqa: E402
    GattLocalCharacteristicParameters, GattCharacteristicProperties as CP, GattProtectionLevel as PL,
    GattLocalDescriptorParameters, GattServiceProviderAdvertisingParameters, GattWriteOption)

buf = blehid.make_buffer(blehid.REPORT_MAP)
assert buf.length == len(blehid.REPORT_MAP), buf.length
p = GattLocalCharacteristicParameters()
p.characteristic_properties = CP.READ | CP.NOTIFY
assert int(p.characteristic_properties) & int(CP.READ)
p.read_protection_level = PL.ENCRYPTION_REQUIRED
p.write_protection_level = PL.PLAIN
p.static_value = blehid.make_buffer(blehid.HID_INFORMATION)
assert p.static_value.length == 4
d = GattLocalDescriptorParameters()
d.static_value = blehid.make_buffer(bytes([1, 1]))
a = GattServiceProviderAdvertisingParameters()
a.is_connectable = True
a.is_discoverable = True
print("write option:", GattWriteOption.WRITE_WITH_RESPONSE)
assert blehid.mouse_report(1, -5, 300, -1) == bytes([1, 0xFB, 0xFF, 0x2C, 0x01, 0xFF])
print("parameters OK")

hid = blehid.BleHid(lambda: None)
hid.start()
for _ in range(40):
    time.sleep(0.25)
    if hid.status in ("error", "waiting", "connected"):
        break
print("status:", hid.status, "|", hid.error)
hid.stop()
for bad in ("TypeError", "AttributeError", "NameError", "ImportError"):
    if bad in hid.error:
        sys.exit("API usage error: " + hid.error)
print("BLE SMOKE OK")
