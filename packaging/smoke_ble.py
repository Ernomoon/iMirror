"""CI check on Windows: the WinRT Bluetooth (HID over GATT) code imports and runs up to the
adapter. GitHub runners have no Bluetooth radio, so RADIO_NOT_AVAILABLE is the expected end."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from imirror import winble  # noqa: E402

print("winrt available:", winble.available())
if not winble.available():
    sys.exit("winrt packages missing")
events = []
link = winble.WinBleLink(lambda k, v: events.append((k, v)))
link.start()
time.sleep(8)
print("events:", events)
print("error:", link.error, "| ready:", link.connected)
link.stop()
bad = [v for k, v in events if k == "esp" and isinstance(v, str) and ("AttributeError" in v or "TypeError" in v
                                                                     or "NameError" in v or "ImportError" in v)]
if bad:
    sys.exit("API usage error: " + "; ".join(bad))
if not events and not link.error:
    sys.exit("no result from Bluetooth init")
print("BLE SMOKE OK (API reachable)")
