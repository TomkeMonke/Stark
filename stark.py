"""Stark - a 'Hey Stark' voice assistant with a Jarvis-style HUD.

Run:  .venv\\Scripts\\python.exe stark.py

The Qt event loop + HUD live on the main thread. A background thread does the
listening, thinking and speaking, and talks to the HUD through Qt signals.

A turn is not one command: after Stark answers he holds the microphone open for
a few seconds, so the next thing said needs no wake word. Cutting him off
mid-sentence does the same thing, immediately.

Timers are the one thing that runs the other way round - Stark speaking without
having been spoken to. They queue up in the Announcer and interrupt the wait
for a wake word, but never a conversation in progress: whatever the user is
in the middle of saying matters more than the pasta.
"""
from __future__ import annotations

import queue
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


class Announcer:
    """Things Stark has to say that nobody asked for just now.

    A timer fires on its own thread, which owns neither the voice nor the HUD.
    So it leaves the line here and sets the event; the worker picks it up the
    next time it is between conversations.
    """

    def __init__(self) -> None:
        self._lines: queue.Queue = queue.Queue()
        self.event = threading.Event()

    def say(self, line: str) -> None:
        self._lines.put(line)
        self.event.set()

    def take(self) -> list[str]:
        """Everything queued, cleared in one go."""
        self.event.clear()
        out = []
        while True:
            try:
                out.append(self._lines.get_nowait())
            except queue.Empty:
                return out


class Controller(QObject):
    appear = Signal()
    vanish = Signal()
    state = Signal(str)
    text = Signal(str)
    followup = Signal(float)


def speak_reply(ctrl: Controller, voice, brain, command: str) -> bool:
    """Answer one command out loud. Returns True if the user cut Stark off.

    When streaming is on, the brain's sentences are piped straight into the
    voice as they are written, so nothing waits for the full reply.
    """
    if not config["stream_speech"]:
        reply = brain.ask(command)
        print(f"[stark] {reply}")
        ctrl.text.emit(reply)
        ctrl.state.emit("speaking")
        return voice.speak(reply)

    said: list[str] = []

    def chunks():
        for piece in brain.ask_stream(command):
            print(f"[stark] {piece}")
            said.append(piece)
            if len(said) == 1:
                ctrl.state.emit("speaking")
            ctrl.text.emit(" ".join(said))
            yield piece

    return voice.speak(chunks())


def conversation(ctrl: Controller, voice, brain, command: str) -> None:
    """Run one exchange and every follow-up that comes out of it.

    Returns once the user has gone quiet - the caller then hides the HUD and
    goes back to waiting for the wake word.
    """
    first = True
    while True:
        if not command:
            if first:  # heard the wake word, then nothing intelligible
                ctrl.state.emit("speaking")
                voice.speak("I didn't catch that, sir.")
            return
        first = False

        print(f"[you]   {command}")
        ctrl.text.emit(command)
        ctrl.state.emit("thinking")

        cut_off = speak_reply(ctrl, voice, brain, command)

        if cut_off:
            # He was interrupted, so the user is already talking. Don't make
            # them wait for a countdown - listen straight away.
            print("[stark] (interrupted)")
            ctrl.text.emit("")
            ctrl.state.emit("listening")
            command = voice.listen_command()
            continue

        window = float(config["followup_window_sec"])
        if not config["followup_enabled"] or window <= 0:
            return

        def on_speech() -> None:
            ctrl.text.emit("")
            ctrl.state.emit("listening")

        ctrl.followup.emit(window)
        command = voice.listen_command(open_window_sec=window, on_speech=on_speech)


def announce(ctrl: Controller, voice, lines: list[str]) -> None:
    """Say something unprompted - a timer going off."""
    if not lines:
        return
    ctrl.text.emit("")
    ctrl.appear.emit()
    ctrl.state.emit("speaking")
    if config["timer_chime"]:
        voice.chime()
    for line in lines:
        print(f"[timer] {line}")
        ctrl.text.emit(line)
        voice.speak(line)
    ctrl.vanish.emit()


def worker(ctrl: Controller, paused: threading.Event,
           wake_event: threading.Event, announcer: Announcer) -> None:
    """Listen -> think -> speak, forever."""
    # Heavy imports happen here so the HUD/Qt can start instantly.
    import timers
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
        print("[stark] started paused - listening is off. Resume it from the tray.")
    else:
        print('[stark] online. Say "Hey Stark".')
        voice.speak("Stark online.")

    # Anything that came due while Stark was closed is announced from here.
    timers.start(announcer.say)

    while True:
        try:
            why = voice.wait_for_wake(is_paused=paused.is_set,
                                      wake_event=wake_event,
                                      announce_event=announcer.event)
            if why == "announce":
                announce(ctrl, voice, announcer.take())
                continue
            ctrl.text.emit("")
            ctrl.state.emit("listening")
            ctrl.appear.emit()
            voice.chime()  # let the user know Stark heard them

            conversation(ctrl, voice, brain, voice.listen_command())
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
        # A local model is a complete substitute for the key, so this is only
        # fatal when there isn't one of those either.
        import local_brain

        if local_brain.available():
            print("[stark] no Gemini key - running on the local model "
                  f"({config['ollama_model']}).")
        else:
            print(
                "No Gemini API key found.\n"
                "Get a free key at https://aistudio.google.com/apikey then either\n"
                '  setx GEMINI_API_KEY "..."   (reopen the terminal afterwards)\n'
                'or add "gemini_api_key" to config.json, then restart Stark.\n'
                "Or run Ollama on this machine and Stark will use that instead."
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
    ctrl.followup.connect(hud.start_followup, Qt.QueuedConnection)

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

    # Push-to-talk: summon Stark without saying his name. Registering the
    # hotkey has to happen on the GUI thread - that's whose message queue
    # Windows posts WM_HOTKEY to.
    wake_event = threading.Event()
    announcer = Announcer()
    hotkey_label = ""
    if config["hotkey_enabled"]:
        import hotkey

        spec = config["hotkey"]
        if hotkey.install(app, spec, wake_event.set):
            hotkey_label = hotkey.pretty(spec)
        app.aboutToQuit.connect(hotkey.remove)

    # System-tray icon so Stark can be paused or quit when running windowless.
    tray = create_tray(
        app, app.quit,
        on_toggle_pause=toggle_pause,
        start_paused=start_paused,
        on_listen_now=wake_event.set,
        hotkey_label=hotkey_label,
    )
    app._tray = tray  # keep a reference alive

    threading.Thread(
        target=worker, args=(ctrl, paused, wake_event, announcer), daemon=True
    ).start()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
