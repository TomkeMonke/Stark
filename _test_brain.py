"""Checks for the streaming brain, against a stubbed Gemini client.

No API key and no requests: a fake client replays chunk sequences shaped like
real ones (built from the SDK's own types, so the part-walking is tested
against the real object model rather than a hand-rolled stand-in).

Run:  .venv\\Scripts\\python.exe _test_brain.py
"""
from __future__ import annotations

import time
import types as pytypes

from google.genai import errors as genai_errors
from google.genai import types

import brain as B
from brain import CHUNK_CHARS, FIRST_CHUNK_CHARS, Brain, _take_sentences

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# ----- a stand-in for the Gemini client ---------------------------------
def chunk(text: str | None = None, call: tuple[str, dict] | None = None):
    """One streamed response, the shape the SDK really hands back."""
    parts = []
    if text is not None:
        parts.append(types.Part(text=text))
    if call is not None:
        parts.append(types.Part(
            function_call=types.FunctionCall(name=call[0], args=call[1])))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=parts))]
    )


class FakeModels:
    def __init__(self, chunks, delay=0.0, error=None) -> None:
        self.chunks, self.delay, self.error = chunks, delay, error
        self.calls = 0

    def generate_content_stream(self, *, model, contents, config):
        self.calls += 1
        if self.error is not None:
            raise self.error

        def gen():
            for c in self.chunks:
                if self.delay:
                    time.sleep(self.delay)
                yield c

        return gen()


def make_brain(chunks, delay=0.0, error=None) -> Brain:
    """A Brain with everything real except the network."""
    b = Brain.__new__(Brain)
    b.model = "fake-model"
    b.history = []
    b._config = None
    b.client = pytypes.SimpleNamespace(models=FakeModels(chunks, delay, error))
    return b


# ----- tests -------------------------------------------------------------
def test_take_sentences() -> None:
    print("\n_take_sentences")
    out, rest = _take_sentences("Opening Chrome now, sir. And the ", FIRST_CHUNK_CHARS)
    check("the opening sentence goes out as soon as it lands",
          out == ["Opening Chrome now, sir."] and rest == "And the ", (out, rest))

    out, rest = _take_sentences("Yes. ", FIRST_CHUNK_CHARS)
    check("a two-word fragment waits for company", out == [] and rest == "Yes. ",
          (out, rest))

    out, rest = _take_sentences("Yes. Chrome is already running, sir. Next ",
                                FIRST_CHUNK_CHARS)
    check("...then merges into the sentence after it",
          out == ["Yes. Chrome is already running, sir."], out)

    out, _ = _take_sentences("Opening Chrome now, sir. And the rest of it. ",
                             CHUNK_CHARS)
    check("later sentences are batched rather than split", out == [], out)

    out, rest = _take_sentences("No punctuation yet", FIRST_CHUNK_CHARS)
    check("an unfinished sentence is never released", out == [], out)

    out, _ = _take_sentences('He said "get on with it." Then he left the room. ',
                             FIRST_CHUNK_CHARS)
    check("a quoted full stop still ends the sentence", len(out) >= 1, out)


def test_streaming_order() -> None:
    print("\nstreamed replies")
    b = make_brain([
        chunk("Right away"), chunk(", sir. I've opened "),
        chunk("Chrome for you. Anything else?"),
    ])
    out = list(b.ask_stream("open chrome"))
    check("the reply arrives in speakable pieces", len(out) >= 2, out)
    check("nothing is dropped",
          "".join(out).replace(" ", "").startswith("Rightaway,sir.I'veopened"),
          out)
    check("the trailing fragment is flushed", out[-1].endswith("Anything else?"), out)
    check("it is still exactly one request", b.client.models.calls == 1)


def test_speaks_before_finishing() -> None:
    print("\nlatency (the whole point)")
    slow = [chunk("Right away, sir, consider it done. "),
            chunk("The file finished downloading about ten minutes ago, "),
            chunk("and I have filed it under Documents for you. ")]
    b = make_brain(slow, delay=0.4)

    t0 = time.monotonic()
    first = last = None
    for piece in b.ask_stream("where is my download"):
        now = time.monotonic() - t0
        if first is None:
            first = now
        last = now
    print(f"    first sentence at {first:.2f}s, last at {last:.2f}s")
    check("the first sentence is out long before the model finishes",
          first < last / 2, f"{first:.2f}s vs {last:.2f}s")

    b = make_brain(slow, delay=0.4)
    whole = time.monotonic()
    "".join(p for p in b.ask_stream("where is my download"))
    whole = time.monotonic() - whole
    check("streaming saves most of the wait before the first word",
          first < whole / 2, f"first {first:.2f}s vs whole reply {whole:.2f}s")


def test_tool_calls() -> None:
    print("\ntool calls")
    seen = {}

    def fake_open(args):
        seen.update(args)
        return "Opening Chrome."

    original = B.DISPATCH
    B.DISPATCH = dict(original, open_app=fake_open)
    try:
        b = make_brain([chunk("Right away, sir, one moment please. "),
                        chunk(call=("open_app", {"name": "chrome"}))])
        out = list(b.ask_stream("open chrome"))
        check("the acknowledgement is spoken before the tool result",
              out[0].startswith("Right away"), out)
        check("the tool's own sentence is spoken last",
              out[-1] == "Opening Chrome.", out)
        check("the tool got its arguments", seen == {"name": "chrome"}, seen)

        roles = [c.role for c in b.history]
        check("history records user, model, tool result, then what was said",
              roles == ["user", "model", "user", "model"], roles)
        check("the remembered turn is what Stark actually said",
              "Opening Chrome." in b.history[-1].parts[0].text,
              b.history[-1].parts[0].text)
    finally:
        B.DISPATCH = original


def test_tool_only_reply() -> None:
    print("\ntool call with no preamble")
    original = B.DISPATCH
    B.DISPATCH = dict(original, system_control=lambda a: "Locking the workstation.")
    try:
        b = make_brain([chunk(call=("system_control", {"action": "lock"}))])
        out = list(b.ask_stream("lock the pc"))
        check("the action still gets spoken", out == ["Locking the workstation."], out)
    finally:
        B.DISPATCH = original


def test_failing_tool() -> None:
    print("\na tool that throws")
    original = B.DISPATCH

    def boom(args):
        raise RuntimeError("no such app")

    B.DISPATCH = dict(original, open_app=boom)
    try:
        b = make_brain([chunk(call=("open_app", {"name": "nope"}))])
        out = list(b.ask_stream("open nope"))
        check("Stark says something rather than going silent",
              len(out) == 1 and "no such app" in out[0], out)
    finally:
        B.DISPATCH = original


def test_errors() -> None:
    print("\nerrors")
    err = genai_errors.ClientError.__new__(genai_errors.ClientError)
    err.code = 429
    b = make_brain([], error=err)
    out = list(b.ask_stream("hello"))
    check("a 429 is spoken, not swallowed",
          len(out) == 1 and "daily request limit" in out[0], out)
    check("the unanswered turn is dropped from history", b.history == [], b.history)

    b = make_brain([], error=RuntimeError("socket closed"))
    out = list(b.ask_stream("hello"))
    check("any other failure is spoken too",
          len(out) == 1 and "socket closed" in out[0], out)
    check("and that turn is dropped as well", b.history == [], b.history)


def test_empty_reply() -> None:
    print("\nempty reply")
    b = make_brain([chunk("")])
    out = list(b.ask_stream("..."))
    check("Stark never returns silence", out == ["Done."], out)


def test_history_bound() -> None:
    print("\nhistory")
    b = make_brain([chunk("Understood, sir, I will keep that in mind. ")])
    for _ in range(20):
        b.client.models.chunks = [chunk("Understood, sir, I will keep that in mind. ")]
        list(b.ask_stream("remember this"))
    check("history stays bounded", len(b.history) <= 26, len(b.history))


def main() -> int:
    test_take_sentences()
    test_streaming_order()
    test_speaks_before_finishing()
    test_tool_calls()
    test_tool_only_reply()
    test_failing_tool()
    test_errors()
    test_empty_reply()
    test_history_bound()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
