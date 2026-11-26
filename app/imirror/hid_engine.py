"""Python twin of the ESP32 firmware HID scheduler (firmware/src/main.cpp).

Used by the built-in-Bluetooth transport: the app sends the same protocol frames whether the
HID device is an ESP32 or the PC's own Bluetooth adapter, and this engine turns them into
HID reports with identical semantics (strict ordering, move splitting, delays, state
re-send). tests/test_all.py checks it report-for-report against the real firmware code.
"""
from __future__ import annotations

import struct
from collections import deque

from . import protocol as P

RID_KEYBOARD, RID_MOUSE, RID_CONSUMER = 1, 2, 3
RESEND_DELAY_MS = 25
QUEUE_SIZE = 384

# Same composite report map as the firmware (keyboard id 1, mouse 8 buttons id 2, consumer id 3).
REPORT_MAP = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, RID_KEYBOARD,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00,
    0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00,
    0xC0,
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, RID_MOUSE,
    0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x08, 0x15, 0x00, 0x25, 0x01,
    0x95, 0x08, 0x75, 0x01, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
    0x05, 0x0C, 0x0A, 0x38, 0x02,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
    0xC0, 0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, RID_CONSUMER,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03,
    0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
])


def _clamp(v, lim):
    return lim if v > lim else (-lim if v < -lim else v)


class HidEngine:
    def __init__(self, interval_ms: int = 8):
        self.interval_us = interval_ms * 1000
        self.q: deque = deque()
        self.last_send_us = 0
        self.delay_running = False
        self.delay_start_ms = 0
        self.buttons = 0
        self.keys = bytes(8)
        self.consumer = 0
        self.resend = {"mouse": False, "keys": False, "consumer": False}
        self.resend_at_ms = 0
        self.connected = False
        self._buf = bytearray()

    # ------------------------------------------------------------------ input (frames)
    def feed(self, data: bytes, now_ms: int = 0):
        self._buf += data
        for cmd, payload in self._take_frames():
            self._frame(cmd, payload, now_ms)

    def _take_frames(self):
        out = []
        buf = self._buf
        i = 0
        while i < len(buf):
            if buf[i] != P.SYNC:
                i += 1
                continue
            if i + 3 > len(buf):
                break
            cmd, ln = buf[i + 1], buf[i + 2]
            if ln > 32:
                i += 1
                continue
            end = i + 3 + ln
            if end + 1 > len(buf):
                break
            payload = bytes(buf[i + 3:end])
            if (cmd + ln + sum(payload)) & 0xFF == buf[end]:
                out.append((cmd, payload))
                i = end + 1
            else:
                i += 1
        del buf[:i]
        return out

    def _push(self, op):
        if op[0] == "move" and self.q:
            tail = self.q[-1]
            if tail[0] == "move" and tail[1] == op[1]:
                tail[2] += op[2]; tail[3] += op[3]; tail[4] += op[4]; tail[5] += op[5]
                return
        if len(self.q) < QUEUE_SIZE:
            self.q.append(op)

    def _frame(self, cmd, p, now_ms):
        if cmd == P.CMD_MOVE and len(p) >= 7:
            dx, dy, w, pan, step = struct.unpack("<hhbbB", p[:7])
            step = 127 if step == 0 else min(step, 127)
            if self.connected:
                self._push(["move", step, dx, dy, w, pan])
        elif cmd == P.CMD_BUTTONS and len(p) >= 1:
            if self.connected:
                self._push(["buttons", p[0]])
        elif cmd == P.CMD_KEYS and len(p) >= 7:
            if self.connected:
                self._push(["keys", bytes([p[0], 0]) + bytes(p[1:7])])
        elif cmd == P.CMD_CONSUMER and len(p) >= 2:
            if self.connected:
                self._push(["consumer", p[0] | (p[1] << 8)])
        elif cmd == P.CMD_DELAY and len(p) >= 2:
            if self.connected:
                self._push(["delay", p[0] | (p[1] << 8)])
        elif cmd == P.CMD_RELEASE:
            self.release_all()
        elif cmd == P.CMD_INTERVAL and len(p) >= 1:
            self.interval_us = max(1, min(50, p[0])) * 1000
        elif cmd == P.CMD_CLEARMOVE:
            self.q = deque(op for op in self.q if op[0] != "move")
            self.delay_running = False

    # ------------------------------------------------------------------ state
    def set_connected(self, on: bool):
        self.connected = on
        if not on:
            self.q.clear()
            self.delay_running = False
            self.buttons = 0
            self.keys = bytes(8)
            self.consumer = 0
            self.resend = dict.fromkeys(self.resend, False)

    def release_all(self, emit=None):
        self.q.clear()
        self.delay_running = False
        self.buttons = 0
        self.keys = bytes(8)
        self.consumer = 0
        self._pending_release = True

    # ------------------------------------------------------------------ output
    def tick(self, now_us: int):
        """Return a list with at most one (report_id, bytes) to send now."""
        if not self.connected:
            return []
        if getattr(self, "_pending_release", False):
            # firmware sends all three zero states immediately, then re-sends once
            self._pending_release = False
            self.resend = dict.fromkeys(self.resend, True)
            self.resend_at_ms = now_us // 1000 + RESEND_DELAY_MS
            return [self._mouse(0, 0, 0, 0), (RID_KEYBOARD, self.keys), self._consumer_report()]
        if (now_us - self.last_send_us) < self.interval_us:
            return []
        now_ms = now_us // 1000
        if any(self.resend.values()) and now_ms - self.resend_at_ms >= 0:
            self.last_send_us = now_us
            if self.resend["mouse"]:
                self.resend["mouse"] = False
                return [self._mouse(0, 0, 0, 0)]
            if self.resend["keys"]:
                self.resend["keys"] = False
                return [(RID_KEYBOARD, self.keys)]
            self.resend["consumer"] = False
            return [self._consumer_report()]

        while self.q:
            op = self.q[0]
            kind = op[0]
            if kind == "delay":
                if not self.delay_running:
                    self.delay_running = True
                    self.delay_start_ms = now_ms
                if now_ms - self.delay_start_ms < op[1]:
                    return []
                self.delay_running = False
                self.q.popleft()
                continue
            if kind == "move":
                step = op[1]
                sx, sy = _clamp(op[2], step), _clamp(op[3], step)
                sw, sp = _clamp(op[4], 127), _clamp(op[5], 127)
                op[2] -= sx; op[3] -= sy; op[4] -= sw; op[5] -= sp
                if not (op[2] or op[3] or op[4] or op[5]):
                    self.q.popleft()
                if sx or sy or sw or sp:
                    self.last_send_us = now_us
                    return [self._mouse(sx, sy, sw, sp)]
                continue
            self.q.popleft()
            self.last_send_us = now_us
            self.resend_at_ms = now_ms + RESEND_DELAY_MS
            if kind == "buttons":
                self.buttons = op[1]
                self.resend["mouse"] = True
                return [self._mouse(0, 0, 0, 0)]
            if kind == "keys":
                self.keys = op[1]
                self.resend["keys"] = True
                return [(RID_KEYBOARD, self.keys)]
            if kind == "consumer":
                self.consumer = op[1]
                self.resend["consumer"] = True
                return [self._consumer_report()]
        return []

    def _mouse(self, dx, dy, w, p):
        return (RID_MOUSE, struct.pack("<Bbbbb", self.buttons, dx, dy, w, p))

    def _consumer_report(self):
        return (RID_CONSUMER, struct.pack("<H", self.consumer))
