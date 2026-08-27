"""Checks for what Stark remembers, what he can see, and what he looks up.

The Gemini client is stubbed, so nothing here makes a request or needs a key.
The screenshot is real - it exercises the actual capture and downscale - but it
only ever reaches the stub.

Run:  .venv\\Scripts\\python.exe _test_tools.py
"""
from __future__ import annotations

import tempfile
import types as pytypes
from pathlib import Path

from google.genai import types

import brain as B
import lookup
import memory

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


class FakeModels:
    """Records what it was asked, and replies with whatever it was given."""

    def __init__(self, reply="Eighteen degrees and overcast.", error=None) -> None:
        self.reply, self.error = reply, error
        self.seen: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.seen.append({"model": model, "contents": contents, "config": config})
        if self.error is not None:
            raise self.error
        return pytypes.SimpleNamespace(text=self.reply)


def stub(reply="Eighteen degrees and overcast.", error=None) -> FakeModels:
    models = FakeModels(reply, error)
    lookup._client = pytypes.SimpleNamespace(models=models)
    return models


# ----- memory ------------------------------------------------------------
def test_memory() -> None:
    print("\nremembering things")
    with tempfile.TemporaryDirectory() as tmp:
        memory.MEMORY_PATH = Path(tmp) / "memory.json"

        check("nothing remembered means nothing in the prompt",
              memory.load() == [] and memory.as_prompt() == "")

        said = memory.remember("parks in level B2")
        check("a fact is acknowledged", said == "I'll remember that.", said)
        check("and survives a reload",
              [f["text"] for f in memory.load()] == ["parks in level B2"],
              memory.load())
        check("the fact is dated", bool(memory.load()[0]["added"]), memory.load())

        check("it reaches the system prompt", "level B2" in memory.as_prompt(),
              memory.as_prompt())

        again = memory.remember("Parks in level B2")
        check("the same fact isn't stored twice",
              again == "I already have that noted, sir." and len(memory.load()) == 1,
              (again, memory.load()))

        memory.remember("sister is called Ada")
        said = memory.forget("sister")
        check("forgetting one is acknowledged", said == "Forgotten.", said)
        check("and only that one goes",
              [f["text"] for f in memory.load()] == ["parks in level B2"],
              memory.load())

        said = memory.forget("something never mentioned")
        check("forgetting what was never known says so",
              "nothing noted" in said, said)

        memory.remember("likes strong coffee")
        said = memory.forget("everything")
        check("everything can be cleared",
              memory.load() == [] and "Forgotten" in said, (said, memory.load()))

        check("an empty fact is refused",
              memory.remember("   ") == "There was nothing to remember, sir.")

        for i in range(memory.MAX_FACTS + 10):
            memory.remember(f"fact number {i}")
        facts = memory.load()
        check("the list is capped", len(facts) == memory.MAX_FACTS, len(facts))
        check("and it is the oldest that go",
              facts[-1]["text"] == f"fact number {memory.MAX_FACTS + 9}",
              facts[-1])

        long = "x" * 500
        memory.forget("everything")
        memory.remember(long)
        check("a very long fact is trimmed",
              len(memory.load()[0]["text"]) == memory.MAX_FACT_CHARS,
              len(memory.load()[0]["text"]))


def test_memory_survives_a_broken_file() -> None:
    print("\na corrupt memory file")
    with tempfile.TemporaryDirectory() as tmp:
        memory.MEMORY_PATH = Path(tmp) / "memory.json"
        memory.MEMORY_PATH.write_text("{not json at all", encoding="utf-8")
        check("it is ignored rather than fatal", memory.load() == [])
        check("and writing still works",
              memory.remember("starts fresh") == "I'll remember that.")


# ----- looking at the screen --------------------------------------------
def test_look_at_screen() -> None:
    print("\nlooking at the screen")
    models = stub("Visual Studio Code, with a Python file open.")
    said = lookup.look_at_screen("what's on my screen")
    check("the answer is spoken back",
          said == "Visual Studio Code, with a Python file open.", said)

    parts = models.seen[0]["contents"][0].parts
    image = [p for p in parts if getattr(p, "inline_data", None)]
    check("a picture of the screen was attached", len(image) == 1, parts)
    check("as a JPEG", image[0].inline_data.mime_type == "image/jpeg",
          image[0].inline_data.mime_type)
    size = len(image[0].inline_data.data)
    print(f"    screenshot attached as {size // 1024} KB of JPEG")
    check("downscaled to something sane", 1000 < size < 900_000, size)
    check("the question went with it",
          any("screen" in (p.text or "") for p in parts if getattr(p, "text", None)),
          parts)
    check("the reply is told to sound spoken",
          "read aloud" in models.seen[0]["config"].system_instruction)


def test_look_at_screen_errors() -> None:
    print("\nwhen looking fails")
    err = Exception()
    err.code = 429
    stub(error=err)
    said = lookup.look_at_screen("what is this")
    check("a rate limit is spoken plainly", "request limit" in said, said)

    stub(error=RuntimeError("boom"))
    said = lookup.look_at_screen("what is this")
    check("any other failure still says something", "couldn't" in said, said)

    stub("   ")
    said = lookup.look_at_screen("what is this")
    check("an empty answer is never silence", "not sure" in said, said)


# ----- answering from the web -------------------------------------------
def test_answer_from_web() -> None:
    print("\nanswering from the web")
    models = stub("Eighteen degrees and overcast in Warsaw.")
    said = lookup.answer_from_web("what's the weather in warsaw")
    check("the answer is spoken back",
          said == "Eighteen degrees and overcast in Warsaw.", said)

    tools = models.seen[0]["config"].tools
    check("google search was actually attached",
          len(tools) == 1 and tools[0].google_search is not None, tools)
    check("the question was passed through",
          models.seen[0]["contents"] == "what's the weather in warsaw",
          models.seen[0]["contents"])

    stub()
    said = lookup.answer_from_web("   ")
    check("an empty question asks rather than searches", "What would you" in said,
          said)

    err = Exception()
    err.code = 429
    stub(error=err)
    said = lookup.answer_from_web("who won")
    check("a rate limit is spoken plainly", "request limit" in said, said)


# ----- wiring ------------------------------------------------------------
def test_tool_wiring() -> None:
    print("\ntool wiring")
    declared = {d.name for d in B.FUNCTION_DECLARATIONS}
    dispatched = set(B.DISPATCH)
    check("every declared tool can be run", declared - dispatched == set(),
          declared - dispatched)
    check("every runnable tool is declared", dispatched - declared == set(),
          dispatched - declared)

    for name in ("look_at_screen", "answer_from_web", "remember", "forget"):
        check(f"{name} is wired up", name in declared and name in dispatched)

    # The arguments the model is told to send must be the ones dispatch reads.
    for decl in B.FUNCTION_DECLARATIONS:
        required = set(decl.parameters.required or [])
        props = set(decl.parameters.properties or {})
        check(f"{decl.name} only requires arguments it declares",
              required <= props, (required, props))


def test_memory_reaches_the_prompt() -> None:
    print("\nremembered facts reach the brain")
    with tempfile.TemporaryDirectory() as tmp:
        memory.MEMORY_PATH = Path(tmp) / "memory.json"
        b = B.Brain.__new__(B.Brain)
        b._tools = []
        before = b._config.system_instruction
        check("nothing is added when there is nothing", "level B2" not in before)

        memory.remember("parks in level B2")
        after = b._config.system_instruction
        check("a new fact is in the very next request's prompt",
              "level B2" in after, after[-200:])


def main() -> int:
    test_memory()
    test_memory_survives_a_broken_file()
    test_look_at_screen()
    test_look_at_screen_errors()
    test_answer_from_web()
    test_tool_wiring()
    test_memory_reaches_the_prompt()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
