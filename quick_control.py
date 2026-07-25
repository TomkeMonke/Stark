"""Bridge to Quick Control, the user's quick-settings panel.

Quick Control owns this machine's screen and sound state — brightness, warm
mode, volume, window layouts — and keeps its own UI in step with it, so Stark
asks the panel to make changes rather than poking the hardware behind its back.
The panel writes its own replies; Stark speaks them as they come.

Transport is the panel's local named pipe (the same one its Claude Code notifier
uses): one JSON object in, one newline-terminated JSON object back. We use plain
file I/O rather than Qt, because this runs on Stark's worker thread — and we do
it inside a helper thread so a busy panel can never wedge the voice loop.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from config import BASE_DIR, config

PIPE_NAME = "QuickControlNotify_v1"
PIPE_PATH = rf"\\.\pipe\{PIPE_NAME}"

STEP = 10            # how far "turn it up/down" moves when no amount is given
TIMEOUT = 6.0        # seconds to wait for an answer
LAUNCH_WAIT = 20.0   # seconds to wait for a cold start to come up

# Spoken verb -> (panel command, fixed arguments). The panel deals in
# primitives (brightness/volume/warm with a level or a step); the wordier verbs
# live here so the brain can pick one name and be done.
VERBS: dict[str, tuple[str, dict]] = {
    "show_panel": ("show", {}),
    "hide_panel": ("hide", {}),
    "status": ("status", {}),
    "brightness": ("brightness", {}),
    "brightness_up": ("brightness", {"delta": STEP}),
    "brightness_down": ("brightness", {"delta": -STEP}),
    "dim": ("dim", {}),
    "volume": ("volume", {}),
    "volume_up": ("volume", {"delta": STEP}),
    "volume_down": ("volume", {"delta": -STEP}),
    "mute": ("mute", {"state": "on"}),
    "unmute": ("mute", {"state": "off"}),
    "app_volume": ("app_volume", {}),
    "mute_app": ("app_volume", {"state": "on"}),
    "unmute_app": ("app_volume", {"state": "off"}),
    "warm_on": ("warm", {"state": "on"}),
    "warm_off": ("warm", {"state": "off"}),
    "warmth": ("warm", {"state": "on"}),
    "preset": ("preset", {}),
    "keep_awake_on": ("awake", {"state": "on"}),
    "keep_awake_off": ("awake", {"state": "off"}),
    "layout": ("layout", {}),
    "list_layouts": ("layout", {}),
}


# --- transport --------------------------------------------------------------
def is_running() -> bool:
    """True if the panel is up and listening."""
    try:
        return PIPE_NAME in os.listdir(r"\\.\pipe")
    except OSError:
        return False


def _talk(payload: dict) -> dict | None:
    """One blocking request/response on the pipe. Raises if it can't connect."""
    with open(PIPE_PATH, "r+b", buffering=0) as pipe:
        pipe.write(json.dumps(payload).encode("utf-8"))
        pipe.flush()
        buf = b""
        while b"\n" not in buf:
            chunk = pipe.read(4096)
            if not chunk:  # the panel hung up
                break
            buf += chunk
    if not buf:
        return None
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "ignore"))


def _request(payload: dict, timeout: float = TIMEOUT) -> tuple[dict | None, str]:
    """Send a payload. Returns (reply, error) — error is "" when all is well.

    The pipe read blocks until the panel answers, so it happens on a throwaway
    thread we can walk away from: a panel stuck behind a modal dialog costs one
    orphaned daemon thread, not a deaf assistant.
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["reply"] = _talk(payload)
        except FileNotFoundError:
            box["error"] = "offline"
        except OSError as exc:
            # 2 = no pipe, 231 = all instances busy, 109 = broken pipe.
            code = getattr(exc, "winerror", 0)
            box["error"] = "offline" if code in (2, 109, 231) else str(exc)
        except Exception as exc:
            box["error"] = str(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None, "timeout"
    error = box.get("error", "")
    reply = box.get("reply")
    if not error and reply is None:
        # Connected, but nothing came back — an older build with no command
        # layer, or a panel that died mid-answer.
        return None, "silent"
    return reply, error


# --- starting the panel -----------------------------------------------------
def _app_dir() -> Path | None:
    candidates = [Path(config["quickcontrol_dir"])] if config["quickcontrol_dir"] else []
    candidates += [BASE_DIR.parent / "QuickControl", Path.home() / "QuickControl"]
    for path in candidates:
        if (path / "main.py").exists():
            return path
    return None


def _launch() -> bool:
    """Start Quick Control and wait for its pipe to appear."""
    base = _app_dir()
    if base is None:
        return False
    exe = base / "dist" / "QuickControl" / "QuickControl.exe"
    pythonw = base / ".venv" / "Scripts" / "pythonw.exe"
    if exe.exists():
        cmd = [str(exe)]
    elif pythonw.exists():
        cmd = [str(pythonw), str(base / "main.py")]
    else:
        return False
    try:
        subprocess.Popen(cmd, cwd=str(base), close_fds=True)
    except Exception:
        return False

    deadline = time.monotonic() + LAUNCH_WAIT
    while time.monotonic() < deadline:
        if is_running():
            time.sleep(0.5)  # let the server finish binding before we knock
            return True
        time.sleep(0.3)
    return False


# --- the bit Stark calls ----------------------------------------------------
def _payload(verb: str, value, target) -> dict | str:
    """Build a panel payload from a spoken verb, or return a complaint."""
    key = str(verb or "").strip().lower().replace(" ", "_").replace("-", "_")
    mapped = VERBS.get(key)
    if mapped is None:
        return f"I don't have a panel command called {verb}, sir."
    cmd, extra = mapped
    payload = {"action": "command", "cmd": cmd, **extra}

    if value is not None and value != "":
        try:
            level = int(float(value))
        except (TypeError, ValueError):
            return f"{value} isn't a level I can work with."
        # For a step verb, an explicit number means "by this much".
        if "delta" in extra:
            payload["delta"] = level if extra["delta"] > 0 else -level
        else:
            payload["value"] = level
    if target:
        payload["target"] = str(target)
    return payload


def command(verb: str, value=None, target=None) -> str:
    """Run one panel command and return a sentence to say out loud."""
    payload = _payload(verb, value, target)
    if isinstance(payload, str):
        return payload

    reply, error = _request(payload)
    if error == "offline" and config["quickcontrol_autostart"]:
        if _launch():
            reply, error = _request(payload)

    if error == "offline":
        return "Quick Control isn't running, sir, and I couldn't start it."
    if error == "silent":
        return "Quick Control is running but isn't answering commands, sir."
    if error:
        return "Quick Control isn't responding, sir."
    return str(reply.get("message") or ("Done." if reply.get("ok") else "That didn't work."))


def try_command(verb: str, value=None, target=None) -> str | None:
    """Like command(), but never starts the panel and gives up quietly.

    For places where Stark has its own fallback — routing volume through the
    panel when it's up, and through the media keys when it isn't. None means
    "use your fallback"; a timeout doesn't, because a slow panel usually runs
    the queued command in the end, and doing it twice would move the volume
    twice as far.
    """
    if not is_running():
        return None
    payload = _payload(verb, value, target)
    if isinstance(payload, str):
        return None
    reply, error = _request(payload, timeout=3.0)
    if error == "timeout":
        return "Quick Control isn't responding, sir."
    if error or not reply or not reply.get("ok"):
        return None
    return str(reply.get("message") or "Done.")
