"""Checks for timers and reminders - including the bit nothing else does,
Stark speaking when nobody asked him to.

Nothing here needs a microphone, a key or sound: the timer file is redirected
into a temporary folder, and the voice is a stand-in that records what it was
told to say.

Run:  .venv\\Scripts\\python.exe _test_timers.py
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import stark
import timers
from config import config

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


class TempTimers:
    """Point timers.json somewhere disposable."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        timers.TIMERS_PATH = Path(self._tmp.name) / "timers.json"
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()


# ----- saying how long ---------------------------------------------------
def test_spoken_duration() -> None:
    print("\nreading a duration aloud")
    cases = {
        1: "1 second",
        45: "45 seconds",
        60: "1 minute",
        90: "1 minute and 30 seconds",
        300: "5 minutes",
        3600: "1 hour",
        3660: "1 hour and 1 minute",
        5400: "1 hour and 30 minutes",
        7200: "2 hours",
    }
    for seconds, want in cases.items():
        got = timers.spoken_duration(seconds)
        check(f"{seconds}s reads as '{want}'", got == want, got)

    check("the odd seconds are dropped from long timers",
          timers.spoken_duration(7203) == "2 hours", timers.spoken_duration(7203))


# ----- setting one -------------------------------------------------------
def test_add() -> None:
    print("\nsetting a timer")
    with TempTimers():
        said = timers.add(300, "the pasta")
        check("the confirmation says both how long and what for",
              said == "Timer set for 5 minutes for the pasta.", said)
        check("and it is on disk", len(timers.load()) == 1, timers.load())

        timers.add(60)
        check("a bare timer needs no label",
              timers.load()[0]["label"] == "", timers.load())
        check("the soonest comes first",
              timers.load()[0]["seconds"] == 60, timers.load())

        check("nonsense is refused", "how long" in timers.add("soon"), timers.add("x"))
        check("zero seconds is refused", "not long enough" in timers.add(0))
        check("longer than a day is refused",
              "up to a day" in timers.add(timers.MAX_SECONDS + 1))

        check("a string of digits still works",
              timers.add("30") == "Timer set for 30 seconds.")


def test_add_is_capped() -> None:
    print("\ntoo many timers")
    with TempTimers():
        for i in range(timers.MAX_TIMERS):
            timers.add(600, f"job {i}")
        said = timers.add(600, "one too many")
        check("the cap is refused out loud", "already have" in said, said)
        check("and nothing extra was stored",
              len(timers.load()) == timers.MAX_TIMERS, len(timers.load()))


# ----- asking about them -------------------------------------------------
def test_describe() -> None:
    print("\nasking what is running")
    with TempTimers():
        check("nothing running says so", timers.describe() == "No timers running, sir.",
              timers.describe())

        timers.add(300, "the pasta")
        said = timers.describe()
        check("one timer reads naturally",
              said == "One timer: 5 minutes left for the pasta.", said)
        check("and what it is for", "the pasta" in said, said)

        # Backdate it a minute: what is left must move, the total must not.
        items = timers.load()
        items[0]["due"] -= 61
        timers._save(items)
        said = timers.describe()
        check("it counts down rather than repeating the total",
              said == "One timer: 4 minutes left for the pasta.", said)
        items[0]["due"] += 61
        timers._save(items)

        timers.add(900, "the meeting")
        said = timers.describe()
        check("two are counted and joined", said.startswith("2 timers:")
              and ", and " in said, said)


# ----- cancelling --------------------------------------------------------
def test_cancel() -> None:
    print("\ncancelling")
    with TempTimers():
        check("with none running he says so",
              timers.cancel("everything") == "You have no timers running, sir.")

        timers.add(300, "the pasta")
        said = timers.cancel("")
        check("'cancel the timer' works when only one is running",
              said == "Timer cancelled for the pasta." and timers.load() == [], said)

        timers.add(300, "the pasta")
        timers.add(900, "the meeting")
        said = timers.cancel("")
        check("with two running he asks which", "Which" in said, said)
        check("and cancels neither", len(timers.load()) == 2, timers.load())

        said = timers.cancel("the meeting timer")
        check("naming one drops only that one, filler words and all",
              said == "Timer cancelled." and
              [t["label"] for t in timers.load()] == ["the pasta"], timers.load())

        said = timers.cancel("the dog")
        check("an unknown label is refused, even with one timer left",
              "no timer for the dog" in said, said)
        check("and that timer is untouched",
              [t["label"] for t in timers.load()] == ["the pasta"], timers.load())

        check("a partial name still finds it",
              timers.cancel("pasta") == "Timer cancelled.", timers.load())

        timers.add(300, "the pasta")
        timers.add(900, "the meeting")
        said = timers.cancel("everything")
        check("everything clears the lot",
              said == "All 2 timers cancelled." and timers.load() == [], said)


# ----- coming due --------------------------------------------------------
def test_due() -> None:
    print("\ncoming due")
    with TempTimers():
        timers.add(300, "the pasta")
        check("nothing is due early", timers.due() == [], timers.due())
        check("and it is still there", len(timers.load()) == 1)

        lines = timers.due(now=time.time() + 301)
        check("it fires once the time is up", len(lines) == 1, lines)
        check("the announcement says how long it was and what for",
              lines[0] == "That's 5 minutes, sir - the pasta.", lines[0])
        check("and it is gone afterwards", timers.load() == [], timers.load())
        check("so it never fires twice", timers.due(now=time.time() + 600) == [])

        timers.add(60)
        lines = timers.due(now=time.time() + 61)
        check("a bare timer announces its length",
              lines[0] == "That's your 1 minute timer, sir.", lines[0])


def test_missed_while_closed() -> None:
    print("\nmissed while Stark was closed")
    with TempTimers():
        timers.add(60, "the washing")
        # Backdate it: due ten minutes ago, as if the PC had been off.
        items = timers.load()
        items[0]["due"] = time.time() - 600
        timers._save(items)

        lines = timers.due()
        check("it is still announced rather than dropped", len(lines) == 1, lines)
        check("and it admits how late it is",
              "went off 10 minutes ago" in lines[0], lines[0])

        timers.add(60, "yesterday's")
        items = timers.load()
        items[0]["due"] = time.time() - timers.STALE_AFTER_SEC - 60
        timers._save(items)
        lines = timers.due()
        check("but a day-old one is dropped silently", lines == [], lines)
        check("and still cleared from the file", timers.load() == [], timers.load())


def test_broken_file() -> None:
    print("\na corrupt timer file")
    with TempTimers():
        timers.TIMERS_PATH.write_text("{not json", encoding="utf-8")
        check("it is ignored rather than fatal", timers.load() == [])
        check("and setting one still works",
              timers.add(60) == "Timer set for 1 minute.")


# ----- the scheduler thread ---------------------------------------------
def test_scheduler_fires() -> None:
    print("\nthe scheduler really goes off")
    with TempTimers():
        fired: list[str] = []
        done = threading.Event()

        def on_fire(line: str) -> None:
            fired.append(line)
            done.set()

        timers.start(on_fire)
        timers.add(1, "the eggs")
        t0 = time.monotonic()
        done.wait(timeout=6)
        late = time.monotonic() - t0 - 1
        print(f"    fired {late:+.2f}s off the mark")
        check("it fires on its own", fired == ["That's 1 second, sir - the eggs."],
              fired)
        check("close to on time", abs(late) < 1.0, f"{late:+.2f}s")
        check("and clears itself", timers.load() == [], timers.load())


# ----- getting it spoken -------------------------------------------------
class FakeSignal:
    def __init__(self) -> None:
        self.sent: list = []

    def emit(self, *args) -> None:
        self.sent.append(args[0] if len(args) == 1 else args)


class FakeCtrl:
    def __init__(self) -> None:
        self.appear = FakeSignal()
        self.vanish = FakeSignal()
        self.state = FakeSignal()
        self.text = FakeSignal()
        self.followup = FakeSignal()


class FakeVoice:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.chimes = 0

    def speak(self, text) -> bool:
        self.spoken.append(text if isinstance(text, str) else " ".join(text))
        return False

    def chime(self) -> None:
        self.chimes += 1


def test_announcer() -> None:
    print("\nthe queue between the timer and the voice")
    a = stark.Announcer()
    check("nothing waiting means nothing to interrupt for", not a.event.is_set())
    check("and nothing to say", a.take() == [])

    a.say("That's 5 minutes, sir.")
    check("a queued line raises the flag", a.event.is_set())

    a.say("And that's the other one.")
    lines = a.take()
    check("both come back in order", len(lines) == 2 and lines[0].startswith("That's"),
          lines)
    check("taking them lowers the flag", not a.event.is_set())
    check("and empties the queue", a.take() == [])


def test_announce_speaks() -> None:
    print("\nsaying it unprompted")
    saved = config.data.get("timer_chime")
    try:
        config.data["timer_chime"] = True
        ctrl, voice = FakeCtrl(), FakeVoice()
        stark.announce(ctrl, voice, ["That's 5 minutes, sir - the pasta."])
        check("the HUD is brought up", ctrl.appear.sent != [], ctrl.appear.sent)
        check("it shows him speaking", "speaking" in ctrl.state.sent, ctrl.state.sent)
        check("the line is spoken",
              voice.spoken == ["That's 5 minutes, sir - the pasta."], voice.spoken)
        check("with a ping first", voice.chimes == 1, voice.chimes)
        check("the HUD is put away again", ctrl.vanish.sent != [], ctrl.vanish.sent)

        config.data["timer_chime"] = False
        ctrl, voice = FakeCtrl(), FakeVoice()
        stark.announce(ctrl, voice, ["one", "two"])
        check("the ping can be turned off", voice.chimes == 0, voice.chimes)
        check("several lines are all spoken", voice.spoken == ["one", "two"],
              voice.spoken)

        ctrl, voice = FakeCtrl(), FakeVoice()
        stark.announce(ctrl, voice, [])
        check("nothing to say means the HUD never appears",
              ctrl.appear.sent == [] and voice.spoken == [], ctrl.appear.sent)
    finally:
        config.data["timer_chime"] = saved


def main() -> int:
    test_spoken_duration()
    test_add()
    test_add_is_capped()
    test_describe()
    test_cancel()
    test_due()
    test_missed_while_closed()
    test_broken_file()
    test_announcer()
    test_announce_speaks()
    test_scheduler_fires()  # last: it leaves a thread running
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
