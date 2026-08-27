"""Checks for window control.

Every verb runs against stand-in windows, because a test has no business
minimizing the windows you are working in. The one thing that is checked
against the real desktop is the filter that decides what counts as a window at
all - that one is only worth testing on real windows, and it only reads.

Run:  .venv\\Scripts\\python.exe _test_windows.py
"""
from __future__ import annotations

import sys

import windows as W

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


class FakeWindow:
    def __init__(self, title: str, minimized: bool = False, breaks: bool = False):
        self.title = title
        self.isMinimized = minimized
        self.isMaximized = False
        self.visible = True
        self._hWnd = abs(hash(title)) % 100000
        self.did: list[str] = []
        self._breaks = breaks

    def _do(self, what: str) -> None:
        if self._breaks:
            raise RuntimeError("access denied")
        self.did.append(what)

    def minimize(self):
        self._do("minimize")

    def maximize(self):
        self._do("maximize")

    def restore(self):
        self._do("restore")

    def close(self):
        self._do("close")

    def activate(self):
        self._do("activate")


class Desktop:
    """Replaces the window list, the active window and the key presses."""

    def __init__(self, wins, active=None, front_fails=False,
                 snap_fails=False):
        self.wins = wins
        self.active = active
        self.front_fails = front_fails
        self.snap_fails = snap_fails
        self.snapped: list[str] = []
        self.keys: list[str] = []

    def __enter__(self):
        self._saved = (W._all, W._active, W._to_front, W._snap)
        W._all = lambda: self.wins
        W._active = lambda: self.active
        W._to_front = self._front
        W._snap = self._snap
        self._keyboard = FakeKeyboard(self.keys)
        self._keyboard.__enter__()
        return self

    def _snap(self, win, side):
        self.snapped.append(side)
        return not self.snap_fails

    def _front(self, win):
        if self.front_fails:
            return False
        win.did.append("front")
        return True

    def __exit__(self, *exc):
        W._all, W._active, W._to_front, W._snap = self._saved
        self._keyboard.__exit__()


class FakeKeyboard:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self._saved = sys.modules.get("pyautogui")
        sys.modules["pyautogui"] = self
        return self

    def hotkey(self, *keys):
        self.log.append("+".join(keys))

    def __exit__(self, *exc):
        if self._saved is None:
            sys.modules.pop("pyautogui", None)
        else:
            sys.modules["pyautogui"] = self._saved


CHROME = "Inbox (3) - Gmail - Google Chrome"
CODE = "brain.py - Stark - Visual Studio Code"
DISCORD = "Friends - Discord"


# ----- reading a title ---------------------------------------------------
def test_app_name() -> None:
    print("\nnaming the app from a window title")
    cases = {
        CHROME: "Google Chrome",
        CODE: "Visual Studio Code",
        "Notepad": "Notepad",
        "report.docx \u2013 Word": "Word",     # en dash, written as an escape
        "photo.png \u2014 Paint": "Paint",     # em dash, likewise
        "Downloads | File Explorer": "File Explorer",
    }
    for title, want in cases.items():
        got = W.app_name(title)
        check(f"{title[:28]!r} is {want}", got == want, got)


# ----- finding the right one --------------------------------------------
def test_find() -> None:
    print("\nfinding the window they meant")
    wins = [FakeWindow(CHROME), FakeWindow(CODE), FakeWindow(DISCORD)]
    with Desktop(wins):
        for said, want in (("chrome", CHROME), ("google chrome", CHROME),
                           ("visual studio code", CODE), ("discord", DISCORD)):
            found = W.find(said)
            check(f"'{said}' finds it", found is not None and found.title == want,
                  found and found.title)

        for alias, want in (("vs code", CODE), ("vscode", CODE), ("code", CODE),
                            ("browser", CHROME)):
            found = W.find(alias)
            check(f"'{alias}' is understood", found is not None
                  and found.title == want, found and found.title)

        check("something that isn't open is not found", W.find("photoshop") is None)
        check("nothing named finds nothing", W.find("") is None)

        found = W.find("gmail")
        check("a word from the page still finds the window",
              found is not None and found.title == CHROME, found and found.title)


def test_find_prefers_the_front() -> None:
    print("\ntwo windows of the same app")
    front, back = FakeWindow("a.py - Visual Studio Code"), \
        FakeWindow("b.py - Visual Studio Code")
    with Desktop([front, back]):
        check("the one in front wins", W.find("vs code") is front)

    # The app name beats a passing mention in someone else's title.
    chat, real = FakeWindow("chrome bug - Discord"), FakeWindow(CHROME)
    with Desktop([chat, real]):
        check("naming the app beats a mention of it", W.find("chrome") is real,
              W.find("chrome").title)


def test_matches_the_executable() -> None:
    print("\nwindows named in another language")
    # A Polish Windows calls Notepad "Notatnik". Nobody says that out loud.
    polish = "Bez tytulu - Notatnik"
    check("the title gives nothing away", W._score("notepad", polish) == 0,
          W._score("notepad", polish))
    check("the executable finds it anyway",
          W._score("notepad", polish, "Notepad.exe") == 5,
          W._score("notepad", polish, "Notepad.exe"))
    check("and that beats every title match",
          W._score("notepad", polish, "Notepad.exe")
          > W._score("notepad", "Untitled - Notepad"))
    check("a made-up handle is not an error", W.process_name(0) == "",
          W.process_name(0))
    check("real windows report a real executable",
          any(W.process_name(w._hWnd) for w in W.real_windows()),
          [W.process_name(w._hWnd) for w in W.real_windows()])


# ----- the verbs ---------------------------------------------------------
def test_verbs_on_a_named_window() -> None:
    print("\nacting on a window by name")
    for action, want in (("minimize", "Minimized"), ("maximize", "Maximized"),
                         ("restore", "Restored"), ("close", "Closed")):
        win = FakeWindow(CHROME)
        with Desktop([win]):
            said = W.control(action, "chrome")
        check(f"{action} acts on it", win.did == [action], win.did)
        check(f"{action} says so by app name",
              said == f"{want} Google Chrome.", said)

    win = FakeWindow(CODE)
    with Desktop([win]):
        said = W.control("focus", "vs code")
    check("focus brings it forward", win.did == ["front"], win.did)
    check("and says so", said == "Bringing Visual Studio Code forward.", said)


def test_verbs_on_the_front_window() -> None:
    print("\nacting on whatever is in front")
    win = FakeWindow(DISCORD)
    with Desktop([win], active=win):
        said = W.control("minimize")
    check("no target means the active window", win.did == ["minimize"], win.did)
    check("and it is named in the reply", "Discord" in said, said)

    with Desktop([], active=None):
        said = W.control("minimize")
    check("with nothing in front he says so", "Nothing is in front" in said, said)

    with Desktop([], active=FakeWindow("   ")):
        check("a nameless window is not something to act on",
              "Nothing is in front" in W.control("maximize"))


def test_snapping() -> None:
    print("\nsnapping to a half")
    win = FakeWindow(CHROME)
    with Desktop([win]) as desk:
        said = W.control("snap_left", "chrome")
    check("it is brought forward first", win.did == ["front"], win.did)
    check("then snapped", desk.snapped == ["left"], desk.snapped)
    check("and reported", said == "Google Chrome to the left.", said)

    win = FakeWindow(CHROME)
    with Desktop([win]) as desk:
        said = W.control("snap_right", "chrome")
    check("the other way too", desk.snapped == ["right"], desk.snapped)

    win = FakeWindow(CHROME)
    with Desktop([win], front_fails=True) as desk:
        said = W.control("snap_left", "chrome")
    check("a window that won't come forward is never snapped blind",
          desk.snapped == [], desk.snapped)
    check("and he says why", "couldn't get hold" in said, said)


def test_snap_that_doesnt_take() -> None:
    print("\na window that won't go where it's put")
    win = FakeWindow(CHROME)
    with Desktop([win], snap_fails=True) as desk:
        said = W.control("snap_left", "chrome")
    check("it was attempted", desk.snapped == ["left"], desk.snapped)
    check("but nothing is claimed that didn't happen",
          said == "Google Chrome wouldn't go to the left, sir.", said)


def test_minimize_all() -> None:
    print("\nclearing the screen")
    with Desktop([FakeWindow(CHROME)]) as desk:
        said = W.control("minimize_all")
    check("show-desktop is pressed", desk.keys == ["win+d"], desk.keys)
    check("and confirmed", said == "Minimizing everything.", said)


def test_listing() -> None:
    print("\nsaying what is open")
    with Desktop([]):
        check("nothing open", W.listing() == "Nothing is open, sir.", W.listing())

    with Desktop([FakeWindow(CHROME)]):
        check("just the one", W.listing() == "Just Google Chrome, sir.", W.listing())

    with Desktop([FakeWindow(CHROME), FakeWindow(CODE), FakeWindow(DISCORD)]):
        said = W.listing()
    check("three are read out",
          said == "3 windows open: Google Chrome, Visual Studio Code and Discord.",
          said)

    with Desktop([FakeWindow(f"job {i} - App {i}") for i in range(9)]):
        said = W.listing()
    check("a screenful is cut short", "and 3 more" in said, said)

    dupes = [FakeWindow("a.py - Visual Studio Code"),
             FakeWindow("b.py - Visual Studio Code")]
    with Desktop(dupes):
        check("the same app isn't said twice",
              W.listing() == "Just Visual Studio Code, sir.", W.listing())


def test_failures() -> None:
    print("\nwhen it can't be done")
    with Desktop([FakeWindow(CHROME)]):
        check("an unknown window is reported",
              "can't find a window for photoshop" in W.control("minimize",
                                                               "photoshop"))
        check("an unknown verb is refused",
              "don't know how to wobble" in W.control("wobble", "chrome"))

    win = FakeWindow(CHROME, breaks=True)
    with Desktop([win]):
        said = W.control("minimize", "chrome")
    check("a window that refuses is reported, not raised",
          said.startswith("I couldn't do that to Google Chrome"), said)

    win = FakeWindow(CODE)
    with Desktop([win], front_fails=True):
        said = W.control("focus", "vs code")
    check("Windows refusing to change focus is admitted",
          "wouldn't let me" in said, said)

    with Desktop([FakeWindow(CHROME)]):
        check("verbs are forgiving about spacing",
              W.control("Snap Left", "chrome").endswith("to the left."))


# ----- the real desktop (read-only) -------------------------------------
def test_real_filter() -> None:
    print("\nwhat counts as a window, on this actual desktop")
    everything = W._all()
    real = W.real_windows()
    print(f"    {len(everything)} windows enumerated, {len(real)} are real ones")
    check("some windows are filtered out", len(real) < len(everything),
          (len(real), len(everything)))
    check("all of them have titles", all(w.title.strip() for w in real))
    check("the desktop itself is not one of them",
          not any(w.title == "Program Manager" for w in real),
          [w.title for w in real])
    check("nothing cloaked survives",
          not any(W._cloaked(w._hWnd) for w in real))
    check("and the listing reads as a sentence",
          W.listing().endswith(".") or W.listing().endswith("sir."), W.listing())


def main() -> int:
    test_app_name()
    test_find()
    test_find_prefers_the_front()
    test_matches_the_executable()
    test_verbs_on_a_named_window()
    test_verbs_on_the_front_window()
    test_snapping()
    test_snap_that_doesnt_take()
    test_minimize_all()
    test_listing()
    test_failures()
    test_real_filter()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
