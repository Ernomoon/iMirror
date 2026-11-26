"""Flashing the prebuilt iMirror firmware into an ESP32 with esptool (in-process, works when frozen)."""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

CHIPS = {
    "esp32": "ESP32 (WROOM / DevKit V1)",
    "esp32s3": "ESP32-S3",
    "esp32c3": "ESP32-C3",
}


def firmware_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "firmware_bin"
    return Path(__file__).resolve().parent.parent.parent / "firmware_bin"


def firmware_path(chip: str) -> Path:
    return firmware_dir() / f"imirror-{chip}.bin"


class _LineWriter(io.TextIOBase):
    def __init__(self, on_line):
        self.on_line = on_line
        self.buf = ""

    def write(self, s):
        self.buf += s.replace("\r", "\n")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.on_line(line.rstrip())
        return len(s)


def flash(chip: str, port: str, on_line) -> bool:
    """Blocking. on_line(str) receives esptool output. Returns True on success."""
    if chip not in CHIPS:
        on_line(f"Неизвестный чип: {chip}")
        return False
    fw = firmware_path(chip)
    if not fw.is_file():
        on_line(f"Нет файла прошивки: {fw}")
        return False
    try:
        import esptool
    except ImportError:
        on_line("esptool не установлен")
        return False
    argv = ["--chip", chip, "--port", port, "--baud", "460800", "write_flash", "0x0", str(fw)]
    on_line("esptool " + " ".join(argv))
    out = _LineWriter(on_line)
    ok = False
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            esptool.main(argv)
        ok = True
    except SystemExit as e:
        ok = e.code in (0, None)
    except Exception as e:      # esptool.FatalError, serial errors
        on_line(f"Ошибка: {e}")
    on_line("Готово: прошивка записана." if ok else
            "Не получилось. Зажмите кнопку BOOT на плате и нажмите «Прошить» ещё раз.")
    return ok
