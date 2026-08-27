"""Render the HUD in each state and save PNGs, to check the visuals by eye."""
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from config import BASE_DIR
from hud import Hud

app = QApplication(sys.argv)
hud = Hud(size=460)
hud.appear()
hud.set_state("listening")
hud.set_text("Hey Stark, open Chrome and search for the weather")


def grab(name: str) -> None:
    for _ in range(8):  # advance the animation a few frames
        hud._tick()
    pm = hud.grab()
    # Composite onto a dark background so transparency reads like the real screen.
    bg = QPixmap(pm.size())
    bg.fill(QColor(12, 14, 18))
    p = QPainter(bg)
    p.drawPixmap(0, 0, pm)
    p.end()
    path = BASE_DIR / f"_hud_{name}.png"
    bg.save(str(path))
    print("saved", path)


def state(name: str, shot: str | None = None) -> None:
    hud.set_state(name)
    grab(shot or name)


def run() -> None:
    state("listening")
    state("thinking")
    state("speaking")

    # The follow-up ring drains over its window; grab it near full and near empty.
    hud.set_text("Chrome is open, sir.")
    hud.start_followup(7.0)
    grab("followup_full")
    hud._followup_start = time.monotonic() - 5.6  # 80% elapsed
    grab("followup_ending")
    app.quit()


QTimer.singleShot(300, run)
app.exec()
