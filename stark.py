"""Stark — a 'Hey Stark' voice assistant with a Jarvis-style HUD.

Run:  .venv\\Scripts\\python.exe stark.py

The Qt event loop + HUD live on the main thread. A background thread does the
listening, thinking and speaking, and talks to the HUD through Qt signals.
"""
from __future__ import annotations

import sys
import threading
import traceback

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication

from config import config, BASE_DIR
from hud import Hud
from tray import create_tray


def _setup_logging() -> None:
    """When launched windowless (pythonw), stdout/stderr are None and any
    print() would crash. Redirect all output to stark.log in that case."""
    if sys.stdout is None or sys.stderr is None:
        log = open(BASE_DIR / "stark.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = log
        sys.stderr = log


class Controller(QObject):
    appear = Signal()
    vanish = Signal()
    state = Signal(str)
    text = Signal(str)


def worker(ctrl: Controller, paused: threading.Event) -> None:
    """Listen → think → speak, forever."""
    # Heavy imports happen here so the HUD/Qt can start instantly.
    from voice import VoiceEngine
    from brain import Brain

    try:
        print("[stark] loading speech engine...")
        voice = VoiceEngine()
        print("[stark] connecting brain...")
        brain = Brain()
    except Exception as exc:
        print(f"[stark] startup failed: {exc}")
        traceback.print_exc()
        return

    if paused.is_set():
        print("[stark] started paused — listening is off. Resume it from the tray.")
    else:
        print('[stark] online. Say "Hey Stark".')
        voice.speak("Stark online.")

    while True:
        try:
            voice.wait_for_wake(is_paused=paused.is_set)
            ctrl.text.emit("")
            ctrl.state.emit("listening")
            ctrl.appear.emit()
            voice.chime()  # let the user know Stark heard them

            command = voice.listen_command()
            if not command:
                ctrl.state.emit("speaking")
                voice.speak("I didn't catch that, sir.")
                ctrl.vanish.emit()
                continue

            print(f"[you]   {command}")
            ctrl.text.emit(command)
            ctrl.state.emit("thinking")

            reply = brain.ask(command)
            print(f"[stark] {reply}")

            ctrl.text.emit(reply)
            ctrl.state.emit("speaking")
            voice.speak(reply)

            ctrl.vanish.emit()
        except Exception as exc:
            print(f"[stark] error: {exc}")
            traceback.print_exc()
            try:
                ctrl.vanish.emit()
            except Exception:
                pass


def main() -> int:
    _setup_logging()
    if not config.api_key:
        print(
            "No Gemini API key found.\n"
            "Get a free key at https://aistudio.google.com/apikey then either\n"
            "  setx GEMINI_API_KEY \"...\"   (reopen the terminal afterwards)\n"
            "or add \"gemini_api_key\" to config.json, then restart Stark."
        )
        return 1

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    hud = Hud(size=config["hud_size"])
    ctrl = Controller()
    ctrl.appear.connect(hud.appear, Qt.QueuedConnection)
    ctrl.vanish.connect(hud.vanish, Qt.QueuedConnection)
    ctrl.state.connect(hud.set_state, Qt.QueuedConnection)
    ctrl.text.connect(hud.set_text, Qt.QueuedConnection)

    # Shared flag: when set, the worker stays dormant and ignores the mic.
    # The last state is remembered in config.json across restarts.
    paused = threading.Event()
    start_paused = bool(config["paused"])
    if start_paused:
        paused.set()

    def toggle_pause(is_paused: bool) -> None:
        paused.set() if is_paused else paused.clear()
        config.data["paused"] = is_paused
        config.save()

    # System-tray icon so Stark can be paused or quit when running windowless.
    tray = create_tray(
        app, app.quit, on_toggle_pause=toggle_pause, start_paused=start_paused
    )
    app._tray = tray  # keep a reference alive

    threading.Thread(target=worker, args=(ctrl, paused), daemon=True).start()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
