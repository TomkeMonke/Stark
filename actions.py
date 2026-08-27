"""Things Stark can do on the machine: open apps/files, browse, control system."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

# Friendly name -> how to launch it. Values are either an executable that
# Windows can resolve on PATH, or a special "shell:" / URL handled below.
APP_ALIASES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "paint": "mspaint",
    "explorer": "explorer",
    "file explorer": "explorer",
    "files": "explorer",
    "settings": "shell:::{ED7BA470-8E54-465E-825C-99712043E01C}",  # falls back below
    "task manager": "taskmgr",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "terminal": "wt",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "spotify": "spotify",
    "discord": "discord",
    "steam": "steam",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "outlook": "outlook",
}


def _run(args: list[str]) -> None:
    subprocess.Popen(args, shell=False)


def _tap(key: str, spoken: str, times: int = 5) -> str:
    """Press a media key a few times (each press is one small step)."""
    import pyautogui

    for _ in range(times):
        pyautogui.press(key)
    return spoken


# Media keys. Every media player on Windows listens to these, so this works
# for Spotify, YouTube in a browser, VLC and anything else, without knowing
# which of them is playing.
MEDIA_KEYS = {
    "play_pause": ("playpause", "Toggled playback."),
    "next_track": ("nexttrack", "Next track."),
    "previous_track": ("prevtrack", "Previous track."),
    "stop": ("stop", "Stopped playback."),
}


def media_control(action: str) -> str:
    """Play/pause, skip or stop whatever is playing."""
    key = MEDIA_KEYS.get(action.strip().lower().replace(" ", "_"))
    if key is None:
        return f"I don't know the media action '{action}'."
    return _tap(key[0], key[1], times=1)


def open_app(name: str) -> str:
    """Launch an application by friendly name."""
    key = name.strip().lower()
    target = APP_ALIASES.get(key, name.strip())

    # Special Windows shell commands (e.g. Settings).
    if key == "settings":
        os.system("start ms-settings:")
        return f"Opening Settings."

    # Direct executable on PATH?
    exe = shutil.which(target)
    if exe:
        _run([exe])
        return f"Opening {name}."

    # Try the Windows "start" resolver (handles Store apps, registered names).
    try:
        os.system(f'start "" "{target}"')
        return f"Opening {name}."
    except Exception as exc:
        return f"I couldn't find {name}. ({exc})"


def open_path(path: str) -> str:
    """Open a file or folder in its default program / Explorer."""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.exists():
        return f"I couldn't find the path {path}."
    os.startfile(str(p))  # type: ignore[attr-defined]
    return f"Opening {p.name}."


def open_url(url: str) -> str:
    """Open a website."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opening {url}."


def web_search(query: str) -> str:
    """Search the web (Google) in the default browser."""
    webbrowser.open(f"https://www.google.com/search?q={quote_plus(query)}")
    return f"Searching the web for {query}."


def type_text(text: str) -> str:
    """Type text into the currently focused window."""
    import pyautogui

    pyautogui.typewrite(text, interval=0.01)
    return "Typed it for you, sir."


def screenshot() -> str:
    """Capture the screen to the Pictures folder."""
    import pyautogui

    shots = Path.home() / "Pictures" / "Stark"
    shots.mkdir(parents=True, exist_ok=True)
    fname = shots / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    pyautogui.screenshot(str(fname))
    return f"Screenshot saved to {fname}."


def quick_control(command: str, value=None, target=None) -> str:
    """Drive the Quick Control panel (screen, sound, window layouts)."""
    import quick_control as panel

    return panel.command(command, value=value, target=target)


def system_control(action: str) -> str:
    """Volume / power / lock controls. `action` is one of the known verbs."""
    import quick_control as panel

    action = action.strip().lower()
    # Volume goes through Quick Control when it's up, so its slider and mute
    # switch stay truthful; the media keys are the fallback when it isn't.
    if action in ("volume_up", "volume up", "louder"):
        return panel.try_command("volume_up") or _tap("volumeup", "Volume up.")
    if action in ("volume_down", "volume down", "quieter"):
        return panel.try_command("volume_down") or _tap("volumedown", "Volume down.")
    if action in ("mute", "unmute"):
        spoken = panel.try_command("mute" if action == "mute" else "unmute")
        return spoken or _tap("volumemute", "Toggled mute.", times=1)
    if action == "lock":
        ctypes.windll.user32.LockWorkStation()
        return "Locking the workstation."
    if action == "sleep":
        os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        return "Going to sleep."
    if action == "shutdown":
        os.system("shutdown /s /t 5")
        return "Shutting down in five seconds."
    if action == "restart":
        os.system("shutdown /r /t 5")
        return "Restarting in five seconds."
    if action == "cancel_shutdown":
        os.system("shutdown /a")
        return "Shutdown cancelled."
    if action == "paste":
        import clipboard

        return clipboard.paste()
    if action == "screenshot":
        return screenshot()
    return f"I don't know the system action '{action}'."
