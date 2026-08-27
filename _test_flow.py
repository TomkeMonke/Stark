"""Checks for the conversation loop: follow-ups, barge recovery, and when
Stark should stay quiet rather than announce that he missed something.

Voice and brain are replaced with stand-ins, so this runs with no microphone,
no API key and no sound.

Run:  .venv\\Scripts\\python.exe _test_flow.py
"""
from __future__ import annotations

import stark
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
    """Hands back queued transcripts and records how it was asked to listen."""

    def __init__(self, commands, interrupt_on=()) -> None:
        self.commands = list(commands)
        self.interrupt_on = set(interrupt_on)
        self.windows: list = []   # the open_window_sec of each listen
        self.spoken: list[str] = []

    def listen_command(self, open_window_sec=None, on_speech=None):
        self.windows.append(open_window_sec)
        return self.commands.pop(0) if self.commands else ""

    def speak(self, text) -> bool:
        self.spoken.append(text if isinstance(text, str) else " ".join(text))
        return (len(self.spoken) - 1) in self.interrupt_on


class FakeBrain:
    def __init__(self, replies=()) -> None:
        self.replies = list(replies)
        self.asked: list[str] = []

    def ask(self, command: str) -> str:
        self.asked.append(command)
        return self.replies.pop(0) if self.replies else "Done."

    def ask_stream(self, command: str):
        yield self.ask(command)


def run(commands, replies=(), interrupt_on=(), **settings):
    saved = {k: config.data.get(k) for k in settings}
    config.data.update(settings)
    try:
        ctrl, voice, brain = FakeCtrl(), FakeVoice(commands, interrupt_on), FakeBrain(replies)
        stark.conversation(ctrl, voice, brain, voice.listen_command())
        return ctrl, voice, brain
    finally:
        config.data.update(saved)


# ----- tests -------------------------------------------------------------
def test_single_exchange() -> None:
    print("\none command, then silence")
    ctrl, voice, brain = run(["open chrome"], ["Opening Chrome."])
    check("the command reaches the brain", brain.asked == ["open chrome"], brain.asked)
    check("the reply is spoken", voice.spoken == ["Opening Chrome."], voice.spoken)
    check("a follow-up window is opened", ctrl.followup.sent == [7.0],
          ctrl.followup.sent)
    check("silence ends it without comment", len(voice.spoken) == 1, voice.spoken)


def test_follow_up_chain() -> None:
    print("\na follow-up with no wake word")
    ctrl, voice, brain = run(
        ["open chrome", "now search for the weather"],
        ["Opening Chrome.", "Searching the web."],
    )
    check("both commands are answered",
          brain.asked == ["open chrome", "now search for the weather"], brain.asked)
    check("both replies are spoken", len(voice.spoken) == 2, voice.spoken)
    check("the follow-up listened on the short window",
          voice.windows[1] == 7.0, voice.windows)
    check("the HUD was told to count down twice", ctrl.followup.sent == [7.0, 7.0],
          ctrl.followup.sent)


def test_missed_command() -> None:
    print("\nwake word, then nothing intelligible")
    ctrl, voice, brain = run([""])
    check("Stark says he missed it", voice.spoken == ["I didn't catch that, sir."],
          voice.spoken)
    check("the brain is never asked", brain.asked == [], brain.asked)
    check("no follow-up window is opened", ctrl.followup.sent == [],
          ctrl.followup.sent)


def test_quiet_after_follow_up() -> None:
    print("\nsilence after an answer")
    _, voice, _ = run(["open chrome", ""], ["Opening Chrome."])
    check("Stark does not announce that he heard nothing",
          voice.spoken == ["Opening Chrome."], voice.spoken)


def test_barge_in() -> None:
    print("\ncut off mid-sentence")
    ctrl, voice, brain = run(
        ["tell me about the weather", "open notepad instead"],
        ["The weather in Warsaw is...", "Opening Notepad."],
        interrupt_on=[0],
    )
    check("the interrupting command is answered",
          brain.asked[-1] == "open notepad instead", brain.asked)
    check("no countdown is shown before the interrupted answer",
          ctrl.followup.sent == [7.0], ctrl.followup.sent)
    check("listening resumes on the full window, not the short one",
          voice.windows[1] is None, voice.windows)
    check("the HUD goes back to listening",
          "listening" in ctrl.state.sent, ctrl.state.sent)


def test_follow_ups_disabled() -> None:
    print("\nfollow-ups turned off")
    ctrl, voice, brain = run(["open chrome", "and the weather"], ["Opening Chrome."],
                             followup_enabled=False)
    check("only the first command is answered", brain.asked == ["open chrome"],
          brain.asked)
    check("no follow-up window is opened", ctrl.followup.sent == [],
          ctrl.followup.sent)
    check("only the wake-word listen happened", len(voice.windows) == 1,
          voice.windows)


def test_zero_window_disables_follow_ups() -> None:
    print("\nfollow-up window set to zero")
    ctrl, _, brain = run(["open chrome", "and the weather"], ["Opening Chrome."],
                         followup_window_sec=0)
    check("zero seconds means off", brain.asked == ["open chrome"], brain.asked)
    check("and no countdown is shown", ctrl.followup.sent == [], ctrl.followup.sent)


def test_non_streaming_path() -> None:
    print("\nstreaming turned off")
    ctrl, voice, brain = run(["open chrome"], ["Opening Chrome."],
                             stream_speech=False)
    check("the whole reply is spoken at once", voice.spoken == ["Opening Chrome."],
          voice.spoken)
    check("the HUD shows the reply text", "Opening Chrome." in ctrl.text.sent,
          ctrl.text.sent)


def main() -> int:
    test_single_exchange()
    test_follow_up_chain()
    test_missed_command()
    test_quiet_after_follow_up()
    test_barge_in()
    test_follow_ups_disabled()
    test_zero_window_disables_follow_ups()
    test_non_streaming_path()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
