"""Binary serial protocol PC -> ESP32 (see firmware/src/main.cpp for the authoritative spec)."""
from __future__ import annotations

import struct

SYNC = 0xA5

CMD_MOVE = 0x01
CMD_BUTTONS = 0x02
CMD_KEYS = 0x03
CMD_CONSUMER = 0x04
CMD_DELAY = 0x05
CMD_RELEASE = 0x06
CMD_PING = 0x07
CMD_INTERVAL = 0x08
CMD_UNPAIR = 0x09
CMD_CLEARMOVE = 0x0A

# Mouse button bits
BTN_LEFT = 0x01
BTN_RIGHT = 0x02
BTN_MIDDLE = 0x04
BTN_BACK = 0x08
BTN_FORWARD = 0x10
BTN_6 = 0x20
BTN_7 = 0x40
BTN_8 = 0x80

# Consumer usages
CONSUMER_VOL_UP = 0x00E9
CONSUMER_VOL_DOWN = 0x00EA
CONSUMER_MUTE = 0x00E2
CONSUMER_PLAY_PAUSE = 0x00CD
CONSUMER_NEXT = 0x00B5
CONSUMER_PREV = 0x00B6
CONSUMER_AC_HOME = 0x0223
CONSUMER_AC_SEARCH = 0x0221

INT16_MAX = 32767


def frame(cmd: int, payload: bytes = b"") -> bytes:
    if not 0 <= cmd <= 0xFF:
        raise ValueError("cmd out of range")
    if len(payload) > 32:
        raise ValueError("payload too long")
    chk = (cmd + len(payload) + sum(payload)) & 0xFF
    return bytes((SYNC, cmd, len(payload))) + payload + bytes((chk,))


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def move(dx: int, dy: int, wheel: int = 0, pan: int = 0, step: int = 127) -> bytes:
    """Relative move. Large values are split into several frames (int16 per frame)."""
    dx, dy, wheel, pan = int(dx), int(dy), int(wheel), int(pan)
    step = _clamp(int(step), 1, 127)
    out = bytearray()
    first = True
    while first or dx or dy or wheel or pan:
        first = False
        fx = _clamp(dx, -INT16_MAX, INT16_MAX)
        fy = _clamp(dy, -INT16_MAX, INT16_MAX)
        fw = _clamp(wheel, -127, 127)
        fp = _clamp(pan, -127, 127)
        out += frame(CMD_MOVE, struct.pack("<hhbbB", fx, fy, fw, fp, step))
        dx -= fx
        dy -= fy
        wheel -= fw
        pan -= fp
    return bytes(out)


def buttons(mask: int) -> bytes:
    return frame(CMD_BUTTONS, bytes((mask & 0xFF,)))


def keys(modifiers: int, usages) -> bytes:
    usages = [u & 0xFF for u in list(usages)[:6]]
    usages += [0] * (6 - len(usages))
    return frame(CMD_KEYS, bytes([modifiers & 0xFF] + usages))


def consumer(usage: int) -> bytes:
    return frame(CMD_CONSUMER, struct.pack("<H", usage & 0xFFFF))


def delay(ms: int) -> bytes:
    ms = max(0, int(ms))
    out = bytearray()
    while ms > 0:
        part = min(ms, 0xFFFF)
        out += frame(CMD_DELAY, struct.pack("<H", part))
        ms -= part
    return bytes(out)


def release() -> bytes:
    return frame(CMD_RELEASE)


def ping() -> bytes:
    return frame(CMD_PING)


def interval(ms: int) -> bytes:
    return frame(CMD_INTERVAL, bytes((_clamp(int(ms), 1, 50),)))


def unpair() -> bytes:
    return frame(CMD_UNPAIR)


def clear_move() -> bytes:
    return frame(CMD_CLEARMOVE)


def parse_frames(data: bytes):
    """Decoder used by the tests (mirrors the firmware parser). Returns list of (cmd, payload)."""
    out = []
    i = 0
    while i < len(data):
        if data[i] != SYNC:
            i += 1
            continue
        if i + 3 > len(data):
            break
        cmd, ln = data[i + 1], data[i + 2]
        end = i + 3 + ln
        if end + 1 > len(data):
            break
        payload = data[i + 3:end]
        chk = data[end]
        if (cmd + ln + sum(payload)) & 0xFF == chk:
            out.append((cmd, bytes(payload)))
            i = end + 1
        else:
            i += 1
    return out
