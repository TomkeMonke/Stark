"""A second brain that runs on this machine, for when Gemini can't.

The free tier has a daily request limit, the key can be missing, and the
internet can be down. Any of those left Stark saying "I'm sorry, sir" to
everything, including "minimize this" - a command that never needed a cloud
model in the first place. So if there is an Ollama server running locally, he
falls back to it and carries on.

The contract is exactly brain.Brain's: one turn in, spoken sentences out,
tool calls executed on the way. Ollama's chat API is close enough to Gemini's
that the conversation so far can be handed straight over, so falling back
mid-conversation keeps the thread rather than starting again.

Nothing here is installed by Stark. If Ollama isn't running, available() says
so once and nothing else changes.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator

from google.genai import types

from config import config

# Long enough for a local model to think, short enough that a hung server
# doesn't hold the microphone open forever. A cold model is loaded into memory
# on the first request, which on a CPU box is most of half a minute.
CHAT_TIMEOUT = 90
PROBE_TIMEOUT = 1.5

# These three tools answer by going back to Gemini, so they are exactly the
# ones that cannot work in the situation that got us here. Offering them to
# the local model is worse than useless: llama3.2 answered "who are you" with
# answer_from_web, which would have made the same request that just failed.
CLOUD_TOOLS = {"answer_from_web", "look_at_screen", "answer_about_clipboard"}

# Small models reach for a tool when they should simply answer, and the more
# tools they are shown the worse it gets - measured on llama3.2, 18 tools got
# 4 of 6 of these right, 15 got 5, 5 tools got 5. Hence the line below, and
# hence dropping the three above rather than passing them on.
LOCAL_ADDENDUM = (
    "\n\nOne more thing, and it matters: only call a tool when the user is "
    "asking you to DO something on this computer. If they are asking a "
    "question, making conversation, or asking about you, answer in words and "
    "call no tool at all."
)


def _url(path: str) -> str:
    return config["ollama_host"].rstrip("/") + path


def _get(path: str, timeout: float):
    with urllib.request.urlopen(_url(path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def available() -> bool:
    """Is there a local server to fall back to? Never raises, never blocks."""
    if not config["ollama_enabled"]:
        return False
    try:
        _get("/api/tags", PROBE_TIMEOUT)
        return True
    except Exception:
        return False


def installed_models() -> list[str]:
    try:
        return [m.get("name", "") for m in _get("/api/tags", PROBE_TIMEOUT)
                .get("models", [])]
    except Exception:
        return []


# ----- translating between the two APIs ---------------------------------
_JSON_TYPES = {
    types.Type.STRING: "string",
    types.Type.INTEGER: "integer",
    types.Type.NUMBER: "number",
    types.Type.BOOLEAN: "boolean",
    types.Type.OBJECT: "object",
    types.Type.ARRAY: "array",
}


def _schema(schema) -> dict:
    """One Gemini schema as plain JSON Schema, which is what Ollama wants."""
    out: dict = {"type": _JSON_TYPES.get(schema.type, "string")}
    if schema.description:
        out["description"] = schema.description
    if schema.enum:
        out["enum"] = list(schema.enum)
    if schema.properties:
        out["properties"] = {k: _schema(v) for k, v in schema.properties.items()}
    if schema.required:
        out["required"] = list(schema.required)
    return out


def tools(cloud_ok: bool = True) -> list[dict]:
    """Stark's tools, in Ollama's shape. The same ones either brain runs,
    minus the ones that need a working Gemini when there isn't one."""
    import brain

    out = []
    for decl in brain.FUNCTION_DECLARATIONS:
        if not cloud_ok and decl.name in CLOUD_TOOLS:
            continue
        params = (_schema(decl.parameters) if decl.parameters is not None
                  else {"type": "object", "properties": {}})
        out.append({"type": "function", "function": {
            "name": decl.name,
            "description": decl.description,
            "parameters": params,
        }})
    return out


def _messages(history: list, system: str) -> list[dict]:
    """The conversation so far, translated from Gemini's Content list.

    Tool results are folded in as plain text rather than Ollama's tool role:
    the local model only needs to know what happened, and the shapes disagree
    on the details in a way that isn't worth defending.
    """
    out = [{"role": "system", "content": system}]
    for content in history:
        text = " ".join(
            part.text.strip() for part in (content.parts or [])
            if getattr(part, "text", None)
        ).strip()
        for part in content.parts or []:
            response = getattr(part, "function_response", None)
            if response is not None:
                result = (response.response or {}).get("result", "")
                text = (text + " " + str(result)).strip()
        if not text:
            continue
        out.append({
            "role": "assistant" if content.role == "model" else "user",
            "content": text,
        })
    return out


def _as_call(text: str) -> dict | None:
    """A function call the model wrote as prose instead of calling.

    Small models do this often enough that the alternative is Stark reading
    a line of JSON out loud in a British accent.
    """
    stripped = text.strip()
    if not stripped.startswith("{") or "name" not in stripped:
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    call = data.get("function") if isinstance(data.get("function"), dict) else data
    name = call.get("name")
    if not name:
        return None
    args = call.get("arguments", call.get("parameters", {}))
    return {"name": name, "arguments": args if isinstance(args, dict) else {}}


# ----- the brain itself --------------------------------------------------
class LocalBrain:
    """Runs one turn against Ollama. Deliberately stateless: the history
    belongs to the Gemini brain, so either can pick up where the other left."""

    def __init__(self, model: str | None = None, cloud_ok: bool = True) -> None:
        self.model = model or config["ollama_model"]
        self.cloud_ok = cloud_ok

    def ask_stream(self, history: list, system: str) -> Iterator[str]:
        import brain

        offered = tools(self.cloud_ok)
        names = {t["function"]["name"] for t in offered}
        body = json.dumps({
            "model": self.model,
            "messages": _messages(history, system + LOCAL_ADDENDUM),
            "tools": offered,
            "stream": True,
        }).encode("utf-8")
        request = urllib.request.Request(
            _url("/api/chat"), data=body,
            headers={"Content-Type": "application/json"},
        )

        calls, buf = [], ""
        spoke = False   # has anything actually been said out loud yet?
        try:
            with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT) as resp:
                for line in resp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        yield f"The local model refused, sir. {chunk['error']}"
                        return

                    message = chunk.get("message") or {}
                    piece = message.get("content") or ""
                    if piece:
                        buf += piece
                        if buf.lstrip().startswith("{"):
                            continue  # might be a tool call in prose; wait
                        ready, buf = brain._take_sentences(
                            buf, brain.CHUNK_CHARS if spoke
                            else brain.FIRST_CHUNK_CHARS)
                        for sentence in ready:
                            spoke = True
                            yield sentence
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        if function.get("name"):
                            calls.append(function)
        except urllib.error.URLError as exc:
            yield f"I couldn't reach the local model either, sir. {exc.reason}"
            return
        except Exception as exc:
            yield f"The local model gave me trouble, sir. {exc}"
            return

        tail = buf.strip()
        if tail:
            written = _as_call(tail)
            if written is None:
                yield tail
                spoke = True
            else:
                calls.append(written)   # a real call, just written out as prose

        runnable = [c for c in calls if c.get("name") in names]

        # What matters from here is whether anything was actually said, not
        # whether the model produced text: a call written out as prose is text
        # and was never spoken.
        if not runnable:
            if calls and not spoke:
                # It asked for a tool it hasn't got, which is what this model
                # does when it wants to answer a question rather than do
                # something. Shown no tools at all it answers perfectly well,
                # so ask again that way rather than refusing.
                yield from self._plainly(history, system)
                return
            if not spoke:
                # Measured: a small model does sometimes return nothing at all
                # for a plain command. "Done." would be a lie about a window
                # that never moved.
                yield "I didn't manage that one, sir."
            return

        for line in self._run(runnable):
            yield line

    def _plainly(self, history: list, system: str) -> Iterator[str]:
        """One more turn with no tools in front of it, for when it clearly
        wanted to talk. Not streamed: it is one short local answer, and the
        request has already cost the user a wait."""
        body = json.dumps({
            "model": self.model,
            "messages": _messages(history, system),
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            _url("/api/chat"), data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT) as resp:
                message = json.loads(resp.read().decode("utf-8")).get("message")
            said = " ".join((message or {}).get("content", "").split())
        except Exception:
            said = ""
        # It can write a call out as prose here too, and this time there is no
        # third attempt to make. Anything but read it aloud.
        if _as_call(said) is not None:
            said = ""
        yield said or "I can't manage that one on the local model, sir."

    @staticmethod
    def _run(calls) -> list[str]:
        import brain

        spoken = []
        for function in calls:
            name = function.get("name", "")
            args = function.get("arguments") or {}
            if isinstance(args, str):        # some models send it as JSON text
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            runner = brain.DISPATCH.get(name)
            if runner is None:
                spoken.append(f"I don't have a {name} to run, sir.")
                continue
            try:
                spoken.append(runner(args))
            except Exception as exc:
                spoken.append(f"Action failed: {exc}")
        return spoken
