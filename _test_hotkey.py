"""Check that push-to-talk really registers and really fires.

Registers the hotkey, synthesizes the keystroke, and waits for the callback -
the only way to know Windows is routing WM_HOTKEY to Qt's message pump.

Run:  .venv\\Scripts\\python.exe _test_hotkey.py
"""
from __future__ import annotations

import ctypes
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import hotkey
from config import config

VK_CONTROL, VK_MENU, KEYUP = 0x11, 0x12, 0x02


def tap(spec: str) -> None:
    """Press the combination as if the user had, via the Windows input queue."""
    mods, vk = hotkey.parse(spec)
    down = []
    if mods & 0x0002:
        down.append(VK_CONTROL)
    if mods & 0x0001:
        down.append(VK_MENU)
    for key in down:
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYUP, 0)
    for key in reversed(down):
        ctypes.windll.user32.keybd_event(key, 0, KEYUP, 0)


def main() -> int:
    spec = config["hotkey"]
    app = QApplication([])
    wake = threading.Event()

    registered = hotkey.install(app, spec, wake.set)
    if not registered:
        print(f"FAIL  could not register {hotkey.pretty(spec)} "
              "(another app already owns it)")
        return 1

    QTimer.singleShot(400, lambda: tap(spec))
    QTimer.singleShot(2500, app.quit)
    app.exec()
    hotkey.remove()

    if wake.is_set():
        print(f"PASS  {hotkey.pretty(spec)} registered and woke Stark")
        return 0
    print(f"FAIL  {hotkey.pretty(spec)} registered but the keypress never arrived")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
