"""Render the HUD in each state and save PNGs, to check the visuals by eye.

The HUD is transparent and sits over whatever is on screen, so every frame is
composited onto a stand-in desktop before it is saved - otherwise the haze and
the bloom can't be judged. Pass --light to check it over a bright desktop,
which is the case the dark scrim exists for.
"""
import math
import random
import sys
import time

from PySide6.QtCore import QTimer, QPointF
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from config import BASE_DIR
from hud import Hud

LIGHT = "--light" in sys.argv

app = QApplication([a for a in sys.argv if not a.startswith("--")])
hud = Hud(size=460)

# A stand-in microphone: a plausible speech envelope, so the voiceprint ring
# is drawn from something with the shape of a real one.
_t0 = time.monotonic()


def fake_level() -> float:
    t = time.monotonic() - _t0
    env = max(0.0, math.sin(t * 1.6)) ** 0.6
    return min(1.0, env * (0.55 + 0.45 * abs(math.sin(t * 9.0)))
               + random.random() * 0.05)


hud.set_level_source(fake_level)
hud.appear()
hud.set_state("listening")
hud.set_text("Hey Stark, open Chrome and search for the weather")


def desktop(size) -> QPixmap:
    """Something with structure behind the HUD, not a flat fill."""
    bg = QPixmap(size)
    g = QLinearGradient(QPointF(0, 0), QPointF(size.width(), size.height()))
    if LIGHT:
        g.setColorAt(0.0, QColor(238, 240, 244))
        g.setColorAt(1.0, QColor(198, 206, 218))
    else:
        g.setColorAt(0.0, QColor(18, 21, 27))
        g.setColorAt(1.0, QColor(9, 11, 15))
    p = QPainter(bg)
    p.fillRect(bg.rect(), g)
    p.setPen(QColor(255, 255, 255, 22 if LIGHT else 14))
    for x in range(0, size.width(), 40):  # a faint grid to show through
        p.drawLine(x, 0, x, size.height())
    for y in range(0, size.height(), 40):
        p.drawLine(0, y, size.width(), y)
    p.end()
    return bg


def grab(name: str, frames: int = 8) -> None:
    for _ in range(frames):  # advance the animation
        hud._tick()
    pm = hud.grab()
    bg = desktop(pm.size())
    p = QPainter(bg)
    p.drawPixmap(0, 0, pm)
    p.end()
    path = BASE_DIR / f"_hud_{name}{'_light' if LIGHT else ''}.png"
    bg.save(str(path))
    print("saved", path)


def settle() -> None:
    """Skip past the assemble animation."""
    hud._boot_start = time.monotonic() - 2.0
    hud._accent = hud._target


def state(name: str, shot: str | None = None) -> None:
    hud.set_state(name)
    settle()
    grab(shot or name)


def run() -> None:
    # Mid-assemble, to check the boot sequence reads.
    hud.set_state("listening")
    hud._boot_start = time.monotonic() - 0.34
    hud._accent = hud._target
    grab("boot", frames=1)

    state("listening")
    state("thinking")
    state("speaking")

    # The follow-up ring drains over its window; grab it near full and near
    # empty. start_followup sets the state itself.
    hud.set_text("Chrome is open, sir.")
    hud.start_followup(7.0)
    settle()
    grab("followup_full")
    hud._followup_start = time.monotonic() - 5.6  # 80% elapsed
    grab("followup_ending")

    # No transcript yet: the moment right after the wake word.
    hud.set_text("")
    state("listening", "wake")
    app.quit()


QTimer.singleShot(300, run)
app.exec()
