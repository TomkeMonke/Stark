"""A global push-to-talk hotkey, so Stark can be summoned without saying his name.

Windows has no cross-process hotkey API in Qt, so we go straight to
``RegisterHotKey`` and read WM_HOTKEY off the Qt event loop's own message queue
via a native event filter. Registering with a NULL window posts the message to
the *thread* queue, which is why this must be set up on the GUI thread - Qt's
dispatcher is the only thing pumping it.

The hotkey simply sets a threading.Event; the voice worker watches that event
alongside the microphone, so a keypress and a spoken wake word are the same
thing as far as the rest of Stark is concerned.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter

_WM_HOTKEY = 0x0312
_MOD_NOREPEAT = 0x4000
_HOTKEY_ID = 0xB1A5  # arbitrary, just has to be unique within this process

_MOD = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002,
        "shift": 0x0004, "win": 0x0008}
_VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): ord(str(d)) for d in range(0, 10)},
    **{f"f{n}": 0x70 + (n - 1) for n in range(1, 13)},
}


def parse(spec: str) -> tuple[int, int] | None:
    """'ctrl+alt+s' -> (modifier mask, virtual key). None if unparseable."""
    mods, vk = 0, None
    for part in (p.strip().lower() for p in spec.split("+")):
        if not part:
            continue
        if part in _MOD:
            mods |= _MOD[part]
        elif part in _VK:
            vk = _VK[part]
    return (mods, vk) if vk is not None else None


def pretty(spec: str) -> str:
    """'ctrl+alt+s' -> 'Ctrl+Alt+S', for menus."""
    return "+".join(p.strip().capitalize() for p in spec.split("+") if p.strip())


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, on_press) -> None:
        super().__init__()
        self._on_press = on_press

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if event_type == b"windows_generic_MSG":
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                self._on_press()
        return False, 0


def install(app, spec: str, on_press) -> bool:
    """Register the hotkey on the GUI thread. Returns False if it's taken.

    A False here is not fatal: another app already owns the combination, and
    Stark still answers to his name.
    """
    parsed = parse(spec)
    if parsed is None:
        print(f"[hotkey] can't parse '{spec}'; push-to-talk disabled.")
        return False
    mods, vk = parsed

    filt = _Filter(on_press)
    app.installNativeEventFilter(filt)
    app._stark_hotkey_filter = filt  # the filter must outlive this call

    ok = bool(ctypes.windll.user32.RegisterHotKey(
        None, _HOTKEY_ID, mods | _MOD_NOREPEAT, vk
    ))
    if ok:
        print(f"[hotkey] push-to-talk on {pretty(spec)}")
    else:
        print(f"[hotkey] {pretty(spec)} is already taken; push-to-talk disabled.")
    return ok


def remove() -> None:
    try:
        ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
    except Exception:
        pass
