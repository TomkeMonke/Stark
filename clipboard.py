"""Reading and writing the Windows clipboard.

Kept apart from actions.py because two different callers need it: the plain
read/write tools here, and lookup.py, which sends the contents back to Gemini
when the user wants something *done* with what they copied rather than just
read back to them.

Everything spoken goes through _speakable: the clipboard is full of newlines,
tabs and thousand-line pastes, and none of that survives a text-to-speech
voice intact.
"""
from __future__ import annotations

from config import config

# What we are willing to hand to the model when asked about the clipboard.
# Comfortably more than anyone pastes deliberately, far less than a log file.
MAX_MODEL_CHARS = 8000


def text() -> str:
    """The clipboard as plain text. "" if it is empty or not text at all."""
    try:
        import pyperclip

        return pyperclip.paste() or ""
    except Exception as exc:
        print(f"[clipboard] could not read: {exc}")
        return ""


def _speakable(raw: str, limit: int) -> str:
    """Collapse whitespace and cut it to something a voice can get through."""
    flat = " ".join(raw.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return f"{cut} ... and it goes on, sir."


def read_aloud() -> str:
    """Say what is on the clipboard."""
    raw = text()
    if not raw.strip():
        return "There's nothing on your clipboard, sir."
    return _speakable(raw, int(config["clipboard_speak_chars"]))


def write(new: str) -> str:
    """Put something on the clipboard."""
    if not str(new or "").strip():
        return "There was nothing to copy, sir."
    try:
        import pyperclip

        pyperclip.copy(new)
    except Exception as exc:
        return f"I couldn't reach the clipboard, sir. {exc}"
    return "Copied to your clipboard."


def paste() -> str:
    """Paste into whatever window has focus."""
    if not text().strip():
        return "There's nothing on your clipboard to paste, sir."
    import pyautogui

    pyautogui.hotkey("ctrl", "v")
    return "Pasted."
