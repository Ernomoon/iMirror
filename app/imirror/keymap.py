"""Windows key events -> USB HID keyboard usages, and text -> key strokes."""
from __future__ import annotations

# ---- physical keys: (scan code set 1, extended flag) -> HID usage -------------------------
_SCAN = {
    0x01: 0x29,  # Esc
    0x02: 0x1E, 0x03: 0x1F, 0x04: 0x20, 0x05: 0x21, 0x06: 0x22,
    0x07: 0x23, 0x08: 0x24, 0x09: 0x25, 0x0A: 0x26, 0x0B: 0x27,  # 1..0
    0x0C: 0x2D, 0x0D: 0x2E, 0x0E: 0x2A, 0x0F: 0x2B,  # - = Backspace Tab
    0x10: 0x14, 0x11: 0x1A, 0x12: 0x08, 0x13: 0x15, 0x14: 0x17,  # Q W E R T
    0x15: 0x1C, 0x16: 0x18, 0x17: 0x0C, 0x18: 0x12, 0x19: 0x13,  # Y U I O P
    0x1A: 0x2F, 0x1B: 0x30, 0x1C: 0x28, 0x1D: 0xE0,  # [ ] Enter LCtrl
    0x1E: 0x04, 0x1F: 0x16, 0x20: 0x07, 0x21: 0x09, 0x22: 0x0A,  # A S D F G
    0x23: 0x0B, 0x24: 0x0D, 0x25: 0x0E, 0x26: 0x0F,  # H J K L
    0x27: 0x33, 0x28: 0x34, 0x29: 0x35, 0x2A: 0xE1, 0x2B: 0x31,  # ; ' ` LShift \
    0x2C: 0x1D, 0x2D: 0x1B, 0x2E: 0x06, 0x2F: 0x19, 0x30: 0x05,  # Z X C V B
    0x31: 0x11, 0x32: 0x10, 0x33: 0x36, 0x34: 0x37, 0x35: 0x38,  # N M , . /
    0x36: 0xE5, 0x37: 0x55, 0x38: 0xE2, 0x39: 0x2C, 0x3A: 0x39,  # RShift KP* LAlt Space Caps
    0x3B: 0x3A, 0x3C: 0x3B, 0x3D: 0x3C, 0x3E: 0x3D, 0x3F: 0x3E,  # F1..F5
    0x40: 0x3F, 0x41: 0x40, 0x42: 0x41, 0x43: 0x42, 0x44: 0x43,  # F6..F10
    0x46: 0x47,  # Scroll Lock
    0x47: 0x5F, 0x48: 0x60, 0x49: 0x61, 0x4A: 0x56,  # KP7 KP8 KP9 KP-
    0x4B: 0x5C, 0x4C: 0x5D, 0x4D: 0x5E, 0x4E: 0x57,  # KP4 KP5 KP6 KP+
    0x4F: 0x59, 0x50: 0x5A, 0x51: 0x5B, 0x52: 0x62, 0x53: 0x63,  # KP1 KP2 KP3 KP0 KP.
    0x56: 0x64,  # ISO extra key (\ |)
    0x57: 0x44, 0x58: 0x45,  # F11 F12
}

_SCAN_EXT = {
    0x1C: 0x58,  # KP Enter
    0x1D: 0xE4,  # RCtrl
    0x35: 0x54,  # KP /
    0x37: 0x46,  # Print Screen
    0x38: 0xE6,  # RAlt (AltGr)
    0x47: 0x4A, 0x48: 0x52, 0x49: 0x4B,  # Home Up PgUp
    0x4B: 0x50, 0x4D: 0x4F,  # Left Right
    0x4F: 0x4D, 0x50: 0x51, 0x51: 0x4E,  # End Down PgDn
    0x52: 0x49, 0x53: 0x4C,  # Insert Delete
    0x5B: 0xE3, 0x5C: 0xE7, 0x5D: 0x65,  # LWin RWin Menu
}

# Keys whose scan code is ambiguous in low-level hooks: decide by virtual-key code.
_VK = {
    0x13: 0x48,  # VK_PAUSE
    0x90: 0x53,  # VK_NUMLOCK
    0x2C: 0x46,  # VK_SNAPSHOT
    0xA0: 0xE1, 0xA1: 0xE5,  # VK_LSHIFT / VK_RSHIFT
    0xA2: 0xE0, 0xA3: 0xE4,  # VK_LCONTROL / VK_RCONTROL
    0xA4: 0xE2, 0xA5: 0xE6,  # VK_LMENU / VK_RMENU
    0x5B: 0xE3, 0x5C: 0xE7,  # VK_LWIN / VK_RWIN
    0xAD: None, 0xAE: None, 0xAF: None,  # volume keys: handled as consumer usages
}

VK_TO_CONSUMER = {
    0xAD: 0x00E2,  # VK_VOLUME_MUTE
    0xAE: 0x00EA,  # VK_VOLUME_DOWN
    0xAF: 0x00E9,  # VK_VOLUME_UP
    0xB0: 0x00B5,  # VK_MEDIA_NEXT_TRACK
    0xB1: 0x00B6,  # VK_MEDIA_PREV_TRACK
    0xB3: 0x00CD,  # VK_MEDIA_PLAY_PAUSE
}

MOD_LCTRL, MOD_LSHIFT, MOD_LALT, MOD_LGUI = 0x01, 0x02, 0x04, 0x08
MOD_RCTRL, MOD_RSHIFT, MOD_RALT, MOD_RGUI = 0x10, 0x20, 0x40, 0x80

_CTRL_CMD_SWAP = {0xE0: 0xE3, 0xE4: 0xE7, 0xE3: 0xE0, 0xE7: 0xE4}


def hid_usage(vk: int, scan: int, extended: bool, swap_ctrl_cmd: bool = False):
    """Return HID usage for a key event, or None if the key is not forwarded."""
    if vk in _VK:
        usage = _VK[vk]
    elif extended:
        usage = _SCAN_EXT.get(scan)
    else:
        usage = _SCAN.get(scan)
    if usage is None:
        return None
    if swap_ctrl_cmd:
        usage = _CTRL_CMD_SWAP.get(usage, usage)
    return usage


def is_modifier(usage: int) -> bool:
    return 0xE0 <= usage <= 0xE7


def modifier_bit(usage: int) -> int:
    return 1 << (usage - 0xE0)


class KeyState:
    """Tracks pressed keys and builds 6-key-rollover keyboard reports."""

    def __init__(self):
        self.modifiers = 0
        self.pressed: list[int] = []

    def press(self, usage: int) -> bool:
        if is_modifier(usage):
            new = self.modifiers | modifier_bit(usage)
            changed = new != self.modifiers
            self.modifiers = new
            return changed
        if usage in self.pressed:
            return False          # auto-repeat from Windows: iOS repeats by itself
        self.pressed.append(usage)
        return True

    def release(self, usage: int) -> bool:
        if is_modifier(usage):
            new = self.modifiers & ~modifier_bit(usage)
            changed = new != self.modifiers
            self.modifiers = new
            return changed
        if usage in self.pressed:
            self.pressed.remove(usage)
            return True
        return False

    def report(self):
        return self.modifiers, self.pressed[-6:]

    def clear(self):
        self.modifiers = 0
        self.pressed.clear()


# ---- text typing --------------------------------------------------------------------------
_US_ROWS = [
    ("`1234567890-=", "~!@#$%^&*()_+", [0x35, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x2D, 0x2E]),
    ("qwertyuiop[]\\", "QWERTYUIOP{}|", [0x14, 0x1A, 0x08, 0x15, 0x17, 0x1C, 0x18, 0x0C, 0x12, 0x13, 0x2F, 0x30, 0x31]),
    ("asdfghjkl;'", "ASDFGHJKL:\"", [0x04, 0x16, 0x07, 0x09, 0x0A, 0x0B, 0x0D, 0x0E, 0x0F, 0x33, 0x34]),
    ("zxcvbnm,./", "ZXCVBNM<>?", [0x1D, 0x1B, 0x06, 0x19, 0x05, 0x11, 0x10, 0x36, 0x37, 0x38]),
]

# Standard Russian (ЙЦУКЕН) layout on the same physical keys.
_RU_ROWS = [
    ("ё1234567890-=", "Ё!\"№;%:?*()_+", [0x35, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x2D, 0x2E]),
    ("йцукенгшщзхъ\\", "ЙЦУКЕНГШЩЗХЪ/", [0x14, 0x1A, 0x08, 0x15, 0x17, 0x1C, 0x18, 0x0C, 0x12, 0x13, 0x2F, 0x30, 0x31]),
    ("фывапролджэ", "ФЫВАПРОЛДЖЭ", [0x04, 0x16, 0x07, 0x09, 0x0A, 0x0B, 0x0D, 0x0E, 0x0F, 0x33, 0x34]),
    ("ячсмитьбю.", "ЯЧСМИТЬБЮ,", [0x1D, 0x1B, 0x06, 0x19, 0x05, 0x11, 0x10, 0x36, 0x37, 0x38]),
]


def _build(rows):
    table = {" ": (0x2C, 0), "\n": (0x28, 0), "\t": (0x2B, 0)}
    for plain, shifted, usages in rows:
        assert len(plain) == len(shifted) == len(usages), (plain, shifted)
        for ch, u in zip(plain, usages):
            table.setdefault(ch, (u, 0))
        for ch, u in zip(shifted, usages):
            table.setdefault(ch, (u, MOD_LSHIFT))
    return table


US_TEXT = _build(_US_ROWS)
RU_TEXT = _build(_RU_ROWS)


def _is_cyrillic(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


def text_to_strokes(text: str):
    """Convert text into (modifiers, usage) strokes.

    The iPhone types with ITS current hardware-keyboard layout, so the whole text is typed
    in one layout: Russian (ЙЦУКЕН) if it contains Cyrillic and no Latin letters, otherwise
    US. Mixed texts use the matching layout per letter (the caller warns the user).
    Returns (strokes, layouts_used, skipped_chars).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    has_cyr = any(_is_cyrillic(ch) for ch in text)
    has_lat = any(("a" <= ch <= "z") or ("A" <= ch <= "Z") for ch in text)
    layouts = set()
    if has_cyr:
        layouts.add("ru")
    if has_lat or not has_cyr:
        layouts.add("us")
    strokes = []
    skipped = []
    for ch in text:
        if has_cyr and not has_lat:
            hit = RU_TEXT.get(ch)
        elif _is_cyrillic(ch):
            hit = RU_TEXT.get(ch)
        else:
            hit = US_TEXT.get(ch)
        if hit is None:
            skipped.append(ch)
            continue
        strokes.append((hit[1], hit[0]))
    return strokes, layouts, skipped
