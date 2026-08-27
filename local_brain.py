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
# doesn't hold the microphone open forever.
CHAT_TIMEOUT = 90
PROBE_TIMEOUT = 1.5


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


def tools() -> list[dict]:
    """Stark's tools, in Ollama's shape. The same list either brain runs."""
    import brain

    out = []
    for decl in brain.FUNCTION_DECLARATIONS:
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


# ----- the brain itself --------------------------------------------------
class LocalBrain:
    """Runs one turn against Ollama. Deliberately stateless: the history
    belongs to the Gemini brain, so either can pick up where the other left."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or config["ollama_model"]

    def ask_stream(self, history: list, system: str) -> Iterator[str]:
        import brain

        body = json.dumps({
            "model": self.model,
            "messages": _messages(history, system),
            "tools": tools(),
            "stream": True,
        }).encode("utf-8")
        request = urllib.request.Request(
            _url("/api/chat"), data=body,
            headers={"Content-Type": "application/json"},
        )

        said, calls, buf = [], [], ""
        spoke = False
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
                        said.append(piece)
                        buf += piece
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
            yield tail

        text = "".join(said).strip()
        if not calls:
            if not text:
                yield "Done."
            return

        for line in self._run(calls):
            yield line

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
