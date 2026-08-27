"""Timers and reminders - the one thing Stark says without being asked.

Everything else in Stark is a reply: the user speaks, Stark answers. A timer is
the exception, so it needs two things nothing else does. It has to survive the
process (a five-minute timer is worthless if it dies when Stark restarts, and
worse than worthless if it dies silently), and it has to be able to interrupt
the idle loop rather than wait for the next wake word.

So the timers live in timers.json, and firing one is a callback into whoever
started the scheduler - stark.py, which owns the voice.
"""
from __future__ import annotations

import json
import re
import threading
import time

from config import BASE_DIR

TIMERS_PATH = BASE_DIR / "timers.json"

MAX_TIMERS = 20
MAX_LABEL_CHARS = 80
MIN_SECONDS = 1
MAX_SECONDS = 24 * 3600

# A timer that came due while the PC was off is still worth mentioning - the
# user asked for it. One that came due last week is just noise.
STALE_AFTER_SEC = 12 * 3600
# Anything later than this is "you were reminded", not "that's your timer".
LATE_AFTER_SEC = 60

_lock = threading.RLock()
_wake = threading.Event()   # nudges the scheduler when the next due time moves
_thread: threading.Thread | None = None


# ----- the file ----------------------------------------------------------
def load() -> list[dict]:
    """Every pending timer, soonest first. Never raises."""
    if not TIMERS_PATH.exists():
        return []
    try:
        data = json.loads(TIMERS_PATH.read_text(encoding="utf-8"))
        items = [t for t in data if isinstance(t, dict) and "due" in t]
    except Exception as exc:
        print(f"[timers] could not read timers.json: {exc}")
        return []
    return sorted(items, key=lambda t: t["due"])


def _save(items: list[dict]) -> None:
    TIMERS_PATH.write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _wake.set()  # the next due time may have moved closer


# ----- saying how long ---------------------------------------------------
def spoken_duration(seconds: float) -> str:
    """"90" -> "1 minute and 30 seconds". Written to be read aloud."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second" + ("" if seconds == 1 else "s")

    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
    if minutes:
        parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
    if secs and not hours:  # nobody wants "2 hours, 5 minutes and 3 seconds"
        parts.append(f"{secs} second" + ("" if secs == 1 else "s"))
    return " and ".join(parts)


def _for(label: str) -> str:
    return f" for {label}" if label else ""


# Words that carry no meaning when working out which timer was meant, so that
# "cancel the pasta timer" finds the one labelled "the pasta".
_FILLER = {"the", "a", "an", "my", "for", "timer", "timers", "reminder",
           "reminders", "please"}


def _key(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", str(text).lower())
             if w not in _FILLER]
    return " ".join(words)


def _matches(needle: str, label: str) -> bool:
    """Either can be the longer phrase: the user says "the pasta timer", the
    label is "the pasta"; or they say "pasta" for "the pasta on the hob"."""
    a, b = _key(needle), _key(label)
    return bool(a) and bool(b) and (a in b or b in a)


# ----- what the tools call ----------------------------------------------
def add(seconds, label: str = "") -> str:
    """Start a timer. Returns what Stark should say about it."""
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "I need to know how long, sir."
    if seconds < MIN_SECONDS:
        return "That's not long enough to time, sir."
    if seconds > MAX_SECONDS:
        return "I only keep timers for up to a day, sir."

    label = " ".join(str(label or "").split())[:MAX_LABEL_CHARS]
    with _lock:
        items = load()
        if len(items) >= MAX_TIMERS:
            return f"I already have {len(items)} timers running, sir."
        items.append({
            "id": int(time.time() * 1000) % 1_000_000,
            "due": time.time() + seconds,
            "seconds": seconds,
            "label": label,
        })
        _save(items)
    return f"Timer set for {spoken_duration(seconds)}{_for(label)}."


def cancel(which: str = "") -> str:
    """Cancel timers matching `which`. Returns what Stark should say."""
    needle = " ".join(str(which or "").split()).lower()
    with _lock:
        items = load()
        if not items:
            return "You have no timers running, sir."

        if needle in ("everything", "all", "all of them", "both"):
            _save([])
            return ("Timer cancelled." if len(items) == 1
                    else f"All {len(items)} timers cancelled.")

        # "cancel the timer" names nothing in particular. With one running
        # that is unambiguous; with several, ask rather than guess.
        if not _key(needle) or needle == "it":
            if len(items) > 1:
                return (f"You have {len(items)} timers running, sir. "
                        "Which would you like cancelled?")
            _save([])
            return f"Timer cancelled{_for(items[0]['label'])}."

        # Naming something that matches nothing is a mishearing, not a licence
        # to cancel whatever happens to be running.
        kept = [t for t in items if not _matches(needle, t["label"])]
        if len(kept) == len(items):
            return f"I have no timer for {which}, sir."
        _save(kept)
        dropped = len(items) - len(kept)
        return ("Timer cancelled." if dropped == 1
                else f"{dropped} timers cancelled.")


def _roughly(seconds: float) -> int:
    """How a person answers "how long left?" - "four minutes", not "three
    minutes and fifty-nine seconds". Under two minutes the seconds matter."""
    seconds = max(0.0, seconds)
    if seconds < 120:
        return int(round(seconds))
    return int(round(seconds / 60.0)) * 60


def describe() -> str:
    """What is running, as a sentence. Reads the remaining time, not the total."""
    items = load()
    if not items:
        return "No timers running, sir."

    now = time.time()
    each = [f"{spoken_duration(_roughly(t['due'] - now))} left{_for(t['label'])}"
            for t in items]
    if len(each) == 1:
        return f"One timer: {each[0]}."
    listed = ", ".join(each[:-1]) + f", and {each[-1]}"
    return f"{len(each)} timers: {listed}."


# ----- the scheduler -----------------------------------------------------
def _announcement(timer: dict, now: float) -> str | None:
    """What to say when a timer comes due, or None if it is too stale to bother."""
    late = now - timer["due"]
    label = timer.get("label", "")
    if late > STALE_AFTER_SEC:
        return None
    if late > LATE_AFTER_SEC:
        # Missed while Stark was closed - say so rather than pretend.
        was = f"Your timer{_for(label)} went off {spoken_duration(late)} ago, sir."
        return was
    if label:
        return f"That's {spoken_duration(timer['seconds'])}, sir - {label}."
    return f"That's your {spoken_duration(timer['seconds'])} timer, sir."


def due(now: float | None = None) -> list[str]:
    """Pop every timer that has come due. Returns the things to say."""
    now = time.time() if now is None else now
    with _lock:
        items = load()
        ready = [t for t in items if t["due"] <= now]
        if not ready:
            return []
        _save([t for t in items if t["due"] > now])
    return [line for line in (_announcement(t, now) for t in ready) if line]


def _run(on_fire) -> None:
    while True:
        # Cleared before the work, not after: a timer added while we are busy
        # here must survive to shorten the sleep below, or a five-second timer
        # set at the wrong moment would wait out the full poll.
        _wake.clear()

        for line in due():
            try:
                on_fire(line)
            except Exception as exc:  # never let one announcement kill the loop
                print(f"[timers] could not announce: {exc}")

        items = load()
        # Sleep until the next one is due - woken early if a timer is added.
        wait = min(items[0]["due"] - time.time(), 30.0) if items else 30.0
        _wake.wait(max(0.05, wait))


def start(on_fire) -> None:
    """Begin watching for due timers, announcing each through `on_fire`.

    Anything that came due while Stark was closed is announced on the first
    pass, with how late it is, so a missed reminder is never silently dropped.
    """
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, args=(on_fire,), daemon=True)
    _thread.start()
