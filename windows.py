"""Moving, sizing and switching between the windows on screen.

Quick Control already handles whole saved arrangements ("arrange my windows
for coding"). This is the other half: one window at a time, by name or
whichever is in front - minimize it, snap it to a half, bring it forward.

Two Windows details do most of the work here. Enumerating windows returns a
great deal that nobody would call a window - the desktop, tool windows, and
the cloaked shells that modern apps leave lying about - so they are filtered
by what Windows itself says about them rather than by title, which would mean
hard-coding names in whatever language the machine happens to run in. And
bringing a window to the front is not something a background process is
allowed to do on request, so it takes the roundabout route below.
"""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes

# Said out loud, so window titles get trimmed to the part that names the
# app: "store-listing.md - Visual Studio Code" is Visual Studio Code. Apps
# separate that with a hyphen, a pipe, or either dash - written as escapes
# below so the only dashes in this repo stay plain ones.
_SPLIT = re.compile("\\s+[-\\u2013\\u2014|]\\s+")

# Names people say that no window ever contains.
ALIASES = {
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "code": "visual studio code",
    "explorer": "file explorer",
    "files": "file explorer",
    "browser": "chrome",
    "teams": "microsoft teams",
    "word": "microsoft word",
    "excel": "microsoft excel",
}

VERBS = ("focus", "minimize", "maximize", "restore", "close",
         "snap_left", "snap_right", "minimize_all", "list")

_DWMWA_CLOAKED = 14
_WS_EX_TOOLWINDOW = 0x00000080
_GWL_EXSTYLE = -20
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MONITOR_DEFAULTTONEAREST = 2
_SW_RESTORE = 9


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


# ----- what counts as a window ------------------------------------------
def _cloaked(hwnd) -> bool:
    """Cloaked means Windows is drawing it nowhere: the input-method window,
    a suspended store app, the second half of a UWP window pair."""
    value = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), _DWMWA_CLOAKED,
            ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        return False
    return bool(value.value)


def process_name(hwnd) -> str:
    """The executable behind a window, without the .exe - "notepad".

    Titles are translated and window titles are not: on a Polish Windows,
    Notepad's window says "Bez tytulu - Notatnik", and nobody asks for that
    out loud. The executable is the same name in every language.
    """
    try:
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.c_ulong(512)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)):
                return ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return ""
    name = buf.value.replace("/", "\\").split("\\")[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


def _tool_window(hwnd) -> bool:
    return bool(ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
                & _WS_EX_TOOLWINDOW)


def _all():
    import pygetwindow as gw

    return gw.getAllWindows()


def _active():
    import pygetwindow as gw

    return gw.getActiveWindow()


def real_windows() -> list:
    """The windows a person would say they have open, front to back."""
    out = []
    for win in _all():
        try:
            if not win.title.strip() or not win.visible:
                continue
            if _cloaked(win._hWnd) or _tool_window(win._hWnd):
                continue
        except Exception:
            continue
        out.append(win)
    return out


# ----- finding the one they meant ---------------------------------------
def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def app_name(title: str) -> str:
    """The app part of a window title - the last dash-separated piece."""
    parts = [p.strip() for p in _SPLIT.split(title) if p.strip()]
    return parts[-1] if parts else title.strip()


def _score(needle: str, title: str, exe: str = "") -> int:
    """How well a spoken name matches a window. 0 means not at all."""
    app, full = _key(app_name(title)), _key(title)
    # process_name() has already dropped the .exe, but take it either way.
    exe = str(exe or "")
    exe = _key(exe[:-4] if exe.lower().endswith(".exe") else exe)
    if not needle:
        return 0
    if needle == exe:      # the surest match there is, and language-proof
        return 5
    if needle == app:
        return 4
    if needle in app.split() or app.startswith(needle):
        return 3
    if needle in app or (exe and needle in exe):
        return 2
    if needle in full:
        return 1
    return 0


def find(name: str):
    """The best window for a spoken name, or None."""
    needle = _key(ALIASES.get(_key(name), name))
    if not needle:
        return None
    best, best_score = None, 0
    for win in real_windows():          # front to back, so ties favour the top
        try:
            score = _score(needle, win.title, process_name(win._hWnd))
        except Exception:
            continue
        if score > best_score:
            best, best_score = win, score
    return best


# ----- bringing one to the front ----------------------------------------
def _to_front(win) -> bool:
    """Windows only lets the foreground process hand focus away, and Stark is
    not it. Restoring first, then a synthetic key tap to lift the foreground
    lock, is what makes SetForegroundWindow actually take."""
    try:
        if win.isMinimized:
            win.restore()
    except Exception:
        pass
    try:
        win.activate()
        return True
    except Exception:
        pass
    try:
        hwnd = win._hWnd
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)      # ALT down
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)      # ALT up
        ctypes.windll.user32.ShowWindow(hwnd, 9)             # SW_RESTORE
        return bool(ctypes.windll.user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def _work_area(hwnd):
    """The usable half-open rectangle of the monitor this window is on,
    taskbar excluded. Returns None if Windows won't say."""
    monitor = ctypes.windll.user32.MonitorFromWindow(
        hwnd, _MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    work = info.rcWork
    return work.left, work.top, work.right - work.left, work.bottom - work.top


def _snap(win, side: str) -> bool:
    """Put a window on one half of its monitor. True if it ended up there.

    The Win+arrow shortcut would be less code, but it is a toggle: sending
    Win+Right to a window already snapped left restores it to the middle, and
    Stark would then announce a snap that didn't happen. Setting the geometry
    means asking for the left half twice does nothing surprising, and the
    result can be checked afterwards.
    """
    area = _work_area(win._hWnd)
    if area is None:
        return False
    left, top, width, height = area
    half = width // 2
    target = (left + half if side == "right" else left, top, half, height)

    ctypes.windll.user32.ShowWindow(win._hWnd, _SW_RESTORE)  # un-maximize
    ctypes.windll.user32.MoveWindow(win._hWnd, *target, True)

    # Some windows refuse to be that small, and a few ignore the move outright.
    slack = max(24, width // 12)
    return (abs(win.left - target[0]) <= slack
            and abs(win.width - target[2]) <= slack)


# ----- what the tool calls ----------------------------------------------
def listing() -> str:
    """What is open, as a sentence."""
    names = []
    for win in real_windows():
        name = app_name(win.title)
        if name not in names:
            names.append(name)
    if not names:
        return "Nothing is open, sir."
    if len(names) == 1:
        return f"Just {names[0]}, sir."

    shown = names[:6]
    rest = len(names) - len(shown)
    if rest:
        return f"{len(names)} windows open: {', '.join(shown)}, and {rest} more."
    listed = ", ".join(shown[:-1]) + f" and {shown[-1]}"
    return f"{len(names)} windows open: {listed}."


def control(action: str, target: str = "") -> str:
    """Act on one window. Blank target means whatever is in front."""
    action = str(action or "").strip().lower().replace(" ", "_")

    if action == "list":
        return listing()

    if action == "minimize_all":
        import pyautogui

        pyautogui.hotkey("win", "d")
        return "Minimizing everything."

    if action not in VERBS:
        return f"I don't know how to {action} a window, sir."

    if target:
        win = find(target)
        if win is None:
            return f"I can't find a window for {target}, sir."
    else:
        win = _active()
        if win is None or not str(getattr(win, "title", "")).strip():
            return "Nothing is in front to act on, sir."

    name = app_name(win.title)
    try:
        if action == "focus":
            return (f"Bringing {name} forward." if _to_front(win)
                    else f"Windows wouldn't let me bring {name} forward, sir.")
        if action == "minimize":
            win.minimize()
            return f"Minimized {name}."
        if action == "maximize":
            win.maximize()
            return f"Maximized {name}."
        if action == "restore":
            win.restore()
            return f"Restored {name}."
        if action == "close":
            win.close()
            return f"Closed {name}."
        if action in ("snap_left", "snap_right"):
            if not _to_front(win):
                return f"I couldn't get hold of {name} to move it, sir."
            side = "left" if action == "snap_left" else "right"
            if not _snap(win, side):
                return f"{name} wouldn't go to the {side}, sir."
            return f"{name} to the {side}."
    except Exception as exc:
        return f"I couldn't do that to {name}, sir. {exc}"
    return f"I don't know how to {action} a window, sir."
