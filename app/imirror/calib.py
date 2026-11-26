"""Geometry + pointer calibration for "click = tap" mode.

The iPhone only understands a *relative* mouse. To tap at an exact spot we:
  1. "home" the pointer: a large move toward the top-left corner (iOS clamps it there);
  2. move by a calibrated number of counts to the target.

Calibration measures how many stream pixels the pointer travels per mouse count.
Gains are stored normalised by the stream's long side, so they survive orientation changes
and different stream resolutions.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


def fit_rect(cx: float, cy: float, cw: float, ch: float, src_w: int, src_h: int) -> Rect:
    """Area occupied by a src_w x src_h video letterboxed (centered) into a client rect."""
    if src_w <= 0 or src_h <= 0 or cw <= 0 or ch <= 0:
        return Rect(cx, cy, cw, ch)
    scale = min(cw / src_w, ch / src_h)
    w, h = src_w * scale, src_h * scale
    return Rect(cx + (cw - w) / 2.0, cy + (ch - h) / 2.0, w, h)


def screen_to_stream(px: float, py: float, video: Rect, src_w: int, src_h: int):
    return ((px - video.x) / video.w * src_w, (py - video.y) / video.h * src_h)


@dataclass
class Calibration:
    gx: float = 0.0          # long-side fraction per count, X axis
    gy: float = 0.0          # long-side fraction per count, Y axis
    step: int = 127          # max counts per HID report used for precise moves
    valid: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        try:
            c = cls(float(d["gx"]), float(d["gy"]), int(d["step"]), bool(d["valid"]))
            if c.gx <= 0 or c.gy <= 0:
                c.valid = False
            return c
        except (KeyError, TypeError, ValueError):
            return cls()

    def px_per_count(self, src_w: int, src_h: int):
        long_side = max(src_w, src_h)
        return self.gx * long_side, self.gy * long_side

    def counts_for(self, dx_px: float, dy_px: float, src_w: int, src_h: int):
        kx, ky = self.px_per_count(src_w, src_h)
        return dx_px / kx, dy_px / ky

    def home_counts(self, src_w: int, src_h: int) -> int:
        """Counts guaranteed to reach a corner from anywhere (with a big safety margin)."""
        kx, ky = self.px_per_count(src_w, src_h)
        k = min(kx, ky)
        need = 1.6 * max(src_w, src_h) / k
        return int(min(32000, max(600, math.ceil(need))))


DEFAULT_HOME_COUNTS = 6000   # used before calibration exists


class CalibrationError(Exception):
    pass


class CalibrationSession:
    """Drives the 3-click calibration.

    Each step: the app homes the pointer to the top-left corner and moves it by
    (counts, counts) with a given HID step size; the user clicks the pointer's centre in
    the mirror window. Points are given in stream pixels.
    """

    SMALL = 8          # step size used to detect pointer acceleration
    FIRST_COUNTS = 40

    def __init__(self, src_w: int, src_h: int):
        self.src_w, self.src_h = src_w, src_h
        self.long = max(src_w, src_h)
        self.results = []      # (counts, step, gx_px, gy_px)
        self.plan = [(self.FIRST_COUNTS, 127)]

    def current(self):
        """(counts, step) for the step the user must click now, or None when finished."""
        i = len(self.results)
        return self.plan[i] if i < len(self.plan) else None

    def add_click(self, sx: float, sy: float):
        cur = self.current()
        if cur is None:
            raise CalibrationError("calibration already finished")
        counts, step = cur
        if not (0 <= sx <= self.src_w and 0 <= sy <= self.src_h):
            raise CalibrationError("Клик вне изображения iPhone. Кликните по центру кружка-указателя.")
        gx, gy = sx / counts, sy / counts
        min_px = 0.01 * self.long
        if sx < min_px or sy < min_px:
            raise CalibrationError("Указатель почти не сдвинулся из угла. Проверьте, что AssistiveTouch "
                                   "включён и iMirror Remote подключён в Bluetooth iPhone.")
        self.results.append((counts, step, gx, gy))
        if len(self.results) == 1:
            # second/third probe: travel ~60% of the short side (diagonal stays on screen)
            target = 0.6 * min(self.src_w, self.src_h)
            c2 = int(max(10, min(30000, round(target / max(gx, gy)))))
            self.plan.append((c2, 127))
            self.plan.append((c2, self.SMALL))

    def finished(self) -> bool:
        return self.current() is None

    def result(self):
        """Returns (Calibration, report_text)."""
        if not self.finished():
            raise CalibrationError("not finished")
        (_, _, g1x, g1y), (_, _, g2x, g2y), (_, _, g3x, g3y) = self.results
        lines = [
            f"короткий ход: {g1x:.3f}/{g1y:.3f} пикс/отсчёт",
            f"длинный ход, быстро: {g2x:.3f}/{g2y:.3f}",
            f"длинный ход, медленно: {g3x:.3f}/{g3y:.3f}",
        ]

        def close(a, b, tol):
            return abs(a - b) <= tol * max(a, b)

        linear = close(g2x, g3x, 0.05) and close(g2y, g3y, 0.05)
        if linear:
            gx, gy, step = (g2x + g3x) / 2, (g2y + g3y) / 2, 127
            lines.append("Ускорение указателя не обнаружено → быстрый режим (шаг 127).")
        else:
            gx, gy, step = g3x, g3y, self.SMALL
            lines.append("Обнаружено ускорение указателя → точный режим (шаг 8, тап чуть медленнее).")
        if not close(g1x, g2x, 0.15) or not close(g1y, g2y, 0.15):
            lines.append("Внимание: короткий и длинный ход заметно различаются — возможна неточность; "
                         "повторите калибровку, кликая точно в центр кружка.")
        cal = Calibration(gx / self.long, gy / self.long, step, True)
        return cal, "\n".join(lines)
