"""Throwaway: render the HUD in a few states and save PNGs to verify visuals."""
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QPainter
from hud import Hud

app = QApplication(sys.argv)
hud = Hud(size=460)
hud.appear()
hud.set_state("listening")
hud.set_text("Hey Stark, open Chrome and search for the weather")

shots = []

def grab(state, name):
    hud.set_state(state)
    # advance animation a few frames
    for _ in range(8):
        hud._tick()
    pm = hud.grab()
    # composite onto dark background so transparency reads like the real screen
    from PySide6.QtGui import QPixmap
    bg = QPixmap(pm.size())
    bg.fill(QColor(12, 14, 18))
    p = QPainter(bg)
    p.drawPixmap(0, 0, pm)
    p.end()
    path = rf"C:\Users\Modern 14\Stark\_hud_{name}.png"
    bg.save(path)
    print("saved", path)

def run():
    grab("listening", "listening")
    grab("thinking", "thinking")
    grab("speaking", "speaking")
    app.quit()

QTimer.singleShot(300, run)
app.exec()
