"""Checks for the local fallback brain, and for Stark actually falling back.

A real HTTP server runs on a spare port and speaks Ollama's protocol - the
same newline-delimited JSON a real server streams - so the parsing, the
streaming and the tool calls are exercised over a real socket. What is not
exercised is a real model: no Ollama is installed here, so what the server
sends is scripted.

Run:  .venv\\Scripts\\python.exe _test_local_brain.py
"""
from __future__ import annotations

import json
import threading
import types as pytypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.genai import errors as genai_errors
from google.genai import types

import brain as B
import local_brain
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


# ----- a stand-in Ollama ------------------------------------------------
class FakeOllama:
    """Serves /api/tags and streams /api/chat, the way Ollama does."""

    def __init__(self, script=None) -> None:
        self.script = script or []
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/api/tags":
                    self.send_error(404)
                    return
                body = json.dumps({"models": [{"name": "llama3.2:latest"}]})
                self._send(body.encode())

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for line in outer.script:
                    payload = line if isinstance(line, bytes) else \
                        json.dumps(line).encode()
                    self.wfile.write(payload + b"\n")
                    self.wfile.flush()

            def _send(self, body: bytes):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._saved = config.data.get("ollama_host")
        config.data["ollama_host"] = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc):
        config.data["ollama_host"] = self._saved
        self.server.shutdown()
        self.server.server_close()


def says(text: str, done: bool = False) -> dict:
    return {"message": {"role": "assistant", "content": text}, "done": done}


def calls(name: str, args: dict) -> dict:
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name,
                                                     "arguments": args}}]},
            "done": False}


def nowhere() -> None:
    """Point the config at a port nothing is listening on."""
    config.data["ollama_host"] = "http://127.0.0.1:1"


# ----- is it there? ------------------------------------------------------
def test_available() -> None:
    print("\nfinding a local model")
    saved = config.data.get("ollama_host")
    try:
        with FakeOllama():
            check("a running server is found", local_brain.available() is True)
            check("and its models can be listed",
                  local_brain.installed_models() == ["llama3.2:latest"],
                  local_brain.installed_models())

        nowhere()
        check("nothing running is not an error, just False",
              local_brain.available() is False)
        check("and the model list is empty", local_brain.installed_models() == [])

        with FakeOllama():
            config.data["ollama_enabled"] = False
            check("turning it off means it is never used",
                  local_brain.available() is False)
    finally:
        config.data["ollama_enabled"] = True
        config.data["ollama_host"] = saved


# ----- translating the tools --------------------------------------------
def test_tool_conversion() -> None:
    print("\nStark's tools in Ollama's shape")
    tools = local_brain.tools()
    check("every tool is offered", len(tools) == len(B.FUNCTION_DECLARATIONS),
          len(tools))
    check("all of them are functions",
          all(t["type"] == "function" and t["function"]["name"] for t in tools))

    by_name = {t["function"]["name"]: t["function"] for t in tools}
    check("the names match the ones dispatch knows",
          set(by_name) == set(B.DISPATCH), set(by_name) ^ set(B.DISPATCH))

    timer = by_name["set_timer"]["parameters"]
    check("an integer argument keeps its type",
          timer["properties"]["seconds"]["type"] == "integer", timer)
    check("required arguments survive", timer["required"] == ["seconds"], timer)

    window = by_name["window_control"]["parameters"]
    check("enums survive, so the model can only pick a real verb",
          set(window["properties"]["action"]["enum"]) == set(__import__(
              "windows").VERBS), window["properties"]["action"])

    check("a tool that takes nothing is still valid JSON schema",
          by_name["list_timers"]["parameters"] == {"type": "object",
                                                   "properties": {}},
          by_name["list_timers"]["parameters"])
    check("every tool describes itself",
          all(f["description"] for f in by_name.values()))


def test_history_conversion() -> None:
    print("\nhanding over the conversation so far")
    history = [
        types.Content(role="user", parts=[types.Part(text="open chrome")]),
        types.Content(role="model", parts=[types.Part(text="Right away, sir.")]),
        types.Content(role="user", parts=[types.Part.from_function_response(
            name="open_app", response={"result": "Opening Chrome."})]),
        types.Content(role="model", parts=[types.Part(text="Opening Chrome.")]),
        types.Content(role="user", parts=[types.Part(text="and the weather")]),
    ]
    out = local_brain._messages(history, "you are stark")
    check("the system prompt leads", out[0] == {"role": "system",
                                                "content": "you are stark"},
          out[0])
    check("roles are translated",
          [m["role"] for m in out[1:]] == ["user", "assistant", "user",
                                           "assistant", "user"],
          [m["role"] for m in out])
    check("what the tool did is passed on as text",
          out[3]["content"] == "Opening Chrome.", out[3])
    check("the last thing said is the new command",
          out[-1]["content"] == "and the weather", out[-1])

    empty = [types.Content(role="model", parts=[])]
    check("an empty turn is dropped rather than sent blank",
          len(local_brain._messages(empty, "x")) == 1)


# ----- answering ---------------------------------------------------------
def one_turn(script, text="hello"):
    history = [types.Content(role="user", parts=[types.Part(text=text)])]
    with FakeOllama(script) as server:
        out = list(local_brain.LocalBrain().ask_stream(history, "system prompt"))
    return out, server


def test_streamed_answer() -> None:
    print("\nanswering from the local model")
    out, server = one_turn([says("Chrome is already "), says("open, sir. "),
                            says("Shall I bring it forward?", done=True)])
    check("the reply comes back in speakable pieces", len(out) >= 1, out)
    check("nothing is lost",
          "".join(out).replace("  ", " ").startswith("Chrome is already open, sir."),
          out)
    check("the trailing question is flushed", out[-1].endswith("forward?"), out)

    sent = server.requests[0]
    check("it asked the configured model", sent["model"] == config["ollama_model"],
          sent["model"])
    check("it streamed", sent["stream"] is True)
    check("the tools went with it", len(sent["tools"]) == len(B.DISPATCH),
          len(sent["tools"]))
    check("and so did the system prompt",
          sent["messages"][0]["content"] == "system prompt", sent["messages"][0])


def test_tool_call() -> None:
    print("\na tool call from the local model")
    seen = {}
    original = B.DISPATCH

    def fake(args):
        seen.update(args)
        return "Minimized Google Chrome."

    B.DISPATCH = dict(original, window_control=fake)
    try:
        out, _ = one_turn([says("One moment, sir, minimizing that now. "),
                           calls("window_control", {"action": "minimize",
                                                    "target": "chrome"})])
        check("the acknowledgement is spoken first",
              out[0].startswith("One moment"), out)
        check("the tool's own sentence comes last",
              out[-1] == "Minimized Google Chrome.", out)
        check("with the arguments the model chose",
              seen == {"action": "minimize", "target": "chrome"}, seen)

        # Smaller models often send the arguments as a JSON string.
        seen.clear()
        out, _ = one_turn([calls("window_control",
                                 '{"action": "maximize", "target": "code"}')])
        check("arguments sent as text are still understood",
              seen == {"action": "maximize", "target": "code"}, seen)

        out, _ = one_turn([calls("summon_a_horse", {})])
        check("a tool that doesn't exist is refused out loud",
              "don't have a summon_a_horse" in out[0], out)
    finally:
        B.DISPATCH = original


def test_local_failures() -> None:
    print("\nwhen the local model misbehaves")
    out, _ = one_turn([b"{not json at all", says("Still here, sir.", done=True)])
    check("a broken line is skipped, not fatal", out == ["Still here, sir."], out)

    out, _ = one_turn([{"error": "model 'llama3.2' not found"}])
    check("an error from the server is spoken",
          len(out) == 1 and "not found" in out[0], out)

    out, _ = one_turn([says("", done=True)])
    check("an empty answer is never silence", out == ["Done."], out)

    saved = config.data.get("ollama_host")
    nowhere()
    try:
        history = [types.Content(role="user", parts=[types.Part(text="hi")])]
        out = list(local_brain.LocalBrain().ask_stream(history, "x"))
        check("a server that vanished mid-turn is reported",
              len(out) == 1 and "local model" in out[0], out)
    finally:
        config.data["ollama_host"] = saved


# ----- Stark falling back ------------------------------------------------
def make_brain(error=None) -> B.Brain:
    """A Brain whose Gemini client always fails."""
    b = B.Brain.__new__(B.Brain)
    b.model = "fake-model"
    b.history = []
    b._tools = []
    b._local_until = 0.0
    b._mentioned_local = False

    def boom(**kwargs):
        raise error

    b.client = pytypes.SimpleNamespace(models=pytypes.SimpleNamespace(
        generate_content_stream=boom))
    return b


def rate_limited():
    err = genai_errors.ClientError.__new__(genai_errors.ClientError)
    err.code = 429
    return err


def test_fallback_on_daily_limit() -> None:
    print("\nout of Gemini requests for the day")
    b = make_brain(error=rate_limited())
    with FakeOllama([says("Chrome is open, sir.", done=True)]) as server:
        out = list(b.ask_stream("is chrome open"))

    check("Stark says he is switching, once",
          out[0] == "I'm switching to the local model, sir.", out)
    check("and the local model answers",
          out[-1] == "Chrome is open, sir.", out)
    check("the daily limit message is never spoken over it",
          not any("daily request limit" in line for line in out), out)
    check("the question reached the local model",
          server.requests[0]["messages"][-1]["content"] == "is chrome open",
          server.requests[0]["messages"][-1])
    check("he stays on the local model for a while",
          b._local_until > 0 and b._prefer_local(), b._local_until)

    with FakeOllama([says("Still local, sir.", done=True)]) as server:
        out = list(b.ask_stream("and now"))
    check("the next turn doesn't bother Gemini at all",
          out == ["Still local, sir."], out)
    check("and he doesn't keep announcing it", len(out) == 1, out)
    check("the conversation carried over",
          len(server.requests[0]["messages"]) > 2,
          server.requests[0]["messages"])


def test_fallback_on_a_dead_connection() -> None:
    print("\nGemini unreachable")
    b = make_brain(error=RuntimeError("socket closed"))
    with FakeOllama([says("I can still hear you, sir.", done=True)]):
        out = list(b.ask_stream("hello"))
    check("the local model takes the turn", out[-1] == "I can still hear you, sir.",
          out)
    check("this one is not treated as a daily limit",
          b._local_until == 0.0, b._local_until)


def test_no_fallback_available() -> None:
    print("\nno local model to fall back to")
    saved = config.data.get("ollama_host")
    nowhere()
    try:
        b = make_brain(error=rate_limited())
        out = list(b.ask_stream("hello"))
        check("the real problem is spoken",
              len(out) == 1 and "daily request limit" in out[0], out)
        check("and the unanswered turn is dropped", b.history == [], b.history)

        b = make_brain(error=RuntimeError("socket closed"))
        out = list(b.ask_stream("hello"))
        check("other failures are spoken too",
              len(out) == 1 and "socket closed" in out[0], out)
    finally:
        config.data["ollama_host"] = saved


def test_local_only() -> None:
    print("\nrunning on the local model by choice")
    saved = config.data.get("ollama_only")
    config.data["ollama_only"] = True
    try:
        b = make_brain(error=RuntimeError("Gemini should never be asked"))
        with FakeOllama([says("Local from the start, sir.", done=True)]):
            out = list(b.ask_stream("hello"))
        check("Gemini is never asked", out == ["Local from the start, sir."], out)
        check("and nothing is announced, because it was the plan",
              not any("switching" in line for line in out), out)
    finally:
        config.data["ollama_only"] = saved


def test_no_brain_at_all() -> None:
    print("\nno key and no local model")
    saved = config.data.get("ollama_host")
    nowhere()
    try:
        b = make_brain(error=RuntimeError("never reached"))
        b.client = None
        out = list(b.ask_stream("hello"))
        check("he says exactly what is wrong",
              len(out) == 1 and "no brain to think with" in out[0], out)
        check("and keeps no half-turn in history", b.history == [], b.history)
    finally:
        config.data["ollama_host"] = saved


def main() -> int:
    test_available()
    test_tool_conversion()
    test_history_conversion()
    test_streamed_answer()
    test_tool_call()
    test_local_failures()
    test_fallback_on_daily_limit()
    test_fallback_on_a_dead_connection()
    test_no_fallback_available()
    test_local_only()
    test_no_brain_at_all()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
