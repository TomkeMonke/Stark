"""Stark's brain: Google Gemini decides what to say and which actions to run.

Uses Gemini function calling. Gemini receives the user's spoken command plus a
set of tools (open app, open url, web search, system control, type text). It
either replies conversationally, calls tools, or both. Runs on Gemini's free
tier - get a key at https://aistudio.google.com/apikey
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import actions
import clipboard
import lookup
import memory
import quick_control
import timers
from config import config

SYSTEM_PROMPT = """You are Stark, a personal AI assistant modeled on J.A.R.V.I.S. \
from Iron Man. You run on the user's Windows PC and are spoken to out loud, so \
your replies are read aloud by a text-to-speech voice.

Personality: calm, intelligent, dry wit, unfailingly polite. Address the user as \
"sir" occasionally but not every sentence. Be concise - one or two short sentences \
is ideal, since everything you say is spoken aloud. Never use markdown, bullet \
points, emoji, or code blocks in your spoken replies.

Capabilities: you can open applications and files, open websites, search the web, \
control the system (volume, lock, sleep, screenshots), type text, work the \
media keys for whatever is playing, and read or set the clipboard. You also \
drive the user's Quick Control panel for screen and sound: brightness, warm \
(night) mode, exact or per-app volume, scene presets, keep-awake, and recalling \
saved window arrangements - use the quick_control tool for all of those, and for \
questions about the current settings. Use the provided tools to take action. If \
the user just wants conversation or an answer, simply reply - do not call a tool.

You can also see and look things up. Use look_at_screen whenever the user refers \
to something they can see but haven't described - an error, a page, "this". Use \
answer_from_web for anything that changes or is recent: news, weather, prices, \
scores, opening hours. Prefer it over answering from memory when being out of \
date would matter; your own knowledge has a cutoff and theirs doesn't.

You remember things across restarts. Use remember when the user tells you to keep \
something, and forget when they ask you to drop it.

Timers and reminders: set_timer takes a number of seconds, so work out the \
duration yourself - "quarter of an hour" is 900, and "remind me at six" is the \
number of seconds between the current time given below and six o'clock. A timer \
goes off whatever you are doing, and even while listening is paused, so never \
say you have set one unless you actually called the tool.

The clipboard: read_clipboard says the contents back word for word and costs \
nothing. Use answer_about_clipboard instead when the user wants something done \
with it - translated, summarized, explained.

When a tool reports back, the user hears that sentence, so don't repeat it.

Safety: for shutdown or restart, briefly confirm what you're about to do in your \
reply. If a request is ambiguous, ask one short clarifying question instead of \
guessing."""

_STR = types.Schema(type=types.Type.STRING)
_INT = types.Schema(type=types.Type.INTEGER)


def _decl(name, description, properties, required):
    # A tool that takes nothing declares no parameters at all: an object schema
    # with an empty property list is not something every model will accept.
    params = types.Schema(
        type=types.Type.OBJECT, properties=properties, required=required
    ) if properties else None
    return types.FunctionDeclaration(
        name=name, description=description, parameters=params
    )


FUNCTION_DECLARATIONS = [
    _decl("open_app",
          "Launch an application by its common name (e.g. 'chrome', 'spotify', 'notepad', 'vs code').",
          {"name": _STR}, ["name"]),
    _decl("open_path",
          "Open a file or folder on disk in its default program or in Explorer.",
          {"path": _STR}, ["path"]),
    _decl("open_url", "Open a website URL in the default browser.",
          {"url": _STR}, ["url"]),
    _decl("web_search",
          "Open a web search in the browser, for when the user wants to look "
          "through results themselves. To answer a question out loud instead, "
          "use answer_from_web.",
          {"query": _STR}, ["query"]),
    _decl("answer_from_web",
          "Answer a question using a live web search: news, weather, prices, "
          "sports results, opening hours, anything that happened recently or "
          "changes often. Use this instead of answering from your own knowledge "
          "whenever the answer could be out of date. Pass the full question.",
          {"question": _STR}, ["question"]),
    _decl("look_at_screen",
          "Look at what is currently on the user's screen and answer a question "
          "about it. Use this for 'what's on my screen', 'what does this error "
          "say', 'read this to me', 'what am I looking at', or any question "
          "about something the user can see but hasn't described. Pass the "
          "question they asked.",
          {"question": _STR}, ["question"]),
    _decl("remember",
          "Store a fact about the user for future conversations, when they say "
          "to remember something. Pass the fact as a short statement, e.g. "
          "'parks in level B2' or 'sister is called Ada'.",
          {"fact": _STR}, ["fact"]),
    _decl("forget",
          "Drop remembered facts matching some words, when the user asks you to "
          "forget something. Pass 'everything' to clear all of them.",
          {"about": _STR}, ["about"]),
    _decl("set_timer",
          "Set a timer or reminder for some number of seconds from now. Work "
          "out the seconds yourself from what the user said. 'label' is what "
          "it is for, as a short phrase that reads naturally after 'for': "
          "'the pasta', 'the meeting', 'taking the washing in'. Leave the "
          "label out for a bare timer.",
          {"seconds": _INT, "label": _STR}, ["seconds"]),
    _decl("cancel_timer",
          "Cancel a running timer. Pass the label of the one to cancel, "
          "'everything' for all of them, or nothing at all when only one is "
          "running.",
          {"which": _STR}, []),
    _decl("list_timers",
          "Say what timers are running and how long is left on each. Use this "
          "for 'how long left', 'what timers do I have', 'is my timer still "
          "going'.",
          {}, []),
    _decl("media_control",
          "Control whatever is currently playing - Spotify, YouTube, a video, "
          "anything at all. Use this for play, pause, resume, skip, next "
          "song, previous song, or stop.",
          {"action": types.Schema(
              type=types.Type.STRING,
              enum=sorted(actions.MEDIA_KEYS))}, ["action"]),
    _decl("read_clipboard",
          "Read out what the user has copied to the clipboard, word for word. "
          "Use this for 'what's on my clipboard' or 'read me what I copied'. "
          "To do something with it instead, use answer_about_clipboard.",
          {}, []),
    _decl("copy_to_clipboard",
          "Put some text on the user's clipboard so they can paste it.",
          {"text": _STR}, ["text"]),
    _decl("answer_about_clipboard",
          "Answer a question about what the user has copied, or transform it: "
          "translate this, summarize what I copied, explain this error, what "
          "does this mean. Pass the question they asked.",
          {"question": _STR}, ["question"]),
    _decl("type_text", "Type the given text into whatever window currently has focus.",
          {"text": _STR}, ["text"]),
    _decl("system_control",
          "Control the system. 'action' is one of: volume_up, volume_down, mute, "
          "lock, sleep, shutdown, restart, cancel_shutdown, paste, screenshot.",
          {"action": types.Schema(
              type=types.Type.STRING,
              enum=["volume_up", "volume_down", "mute", "lock", "sleep",
                    "shutdown", "restart", "cancel_shutdown", "paste",
                    "screenshot"])},
          ["action"]),
    _decl("quick_control",
          "Screen, sound and window controls, through the user's Quick Control "
          "panel. Use this for anything about brightness, warm or night mode, "
          "exact volume levels, one app's volume, keeping the PC awake, "
          "arranging windows, or opening the panel itself. "
          "'value' is a 0-100 level - or, with brightness_up / brightness_down / "
          "volume_up / volume_down, how much to move by. "
          "'target' names a preset (Day, Movie, Night) for 'preset', an app "
          "(e.g. 'spotify') for the app volume commands, or a saved window "
          "arrangement for 'layout'. Use 'status' to answer questions about the "
          "current brightness, volume or screen settings.",
          {"command": types.Schema(type=types.Type.STRING,
                                   enum=sorted(quick_control.VERBS)),
           "value": types.Schema(type=types.Type.INTEGER),
           "target": _STR},
          ["command"]),
]

# Map tool names to the python callables in actions.py.
DISPATCH = {
    "open_app": lambda i: actions.open_app(i["name"]),
    "open_path": lambda i: actions.open_path(i["path"]),
    "open_url": lambda i: actions.open_url(i["url"]),
    "web_search": lambda i: actions.web_search(i["query"]),
    "answer_from_web": lambda i: lookup.answer_from_web(i["question"]),
    "look_at_screen": lambda i: lookup.look_at_screen(i["question"]),
    "remember": lambda i: memory.remember(i["fact"]),
    "forget": lambda i: memory.forget(i["about"]),
    "set_timer": lambda i: timers.add(i["seconds"], i.get("label", "")),
    "cancel_timer": lambda i: timers.cancel(i.get("which", "")),
    "list_timers": lambda i: timers.describe(),
    "media_control": lambda i: actions.media_control(i["action"]),
    "read_clipboard": lambda i: clipboard.read_aloud(),
    "copy_to_clipboard": lambda i: clipboard.write(i["text"]),
    "answer_about_clipboard": lambda i: lookup.answer_about_clipboard(i["question"]),
    "type_text": lambda i: actions.type_text(i["text"]),
    "system_control": lambda i: actions.system_control(i["action"]),
    "quick_control": lambda i: actions.quick_control(
        i["command"], i.get("value"), i.get("target")
    ),
}


# A sentence ends at punctuation followed by space.
#
# How much to release at a time is a measured trade-off, not a guess. The voice
# service takes about the same time to synthesize a clip whatever its length,
# and pads every clip with roughly a second of silence at each end - so each
# extra split buys nothing and costs about a second of dead air mid-reply.
# Only the first sentence is worth rushing out, because that is the one the
# user is sitting waiting for; after that, prefer fewer and longer clips.
_BOUNDARY = re.compile(r"[.!?]+[\"')\]]*\s+")
FIRST_CHUNK_CHARS = 15
CHUNK_CHARS = 120


def _take_sentences(buf: str, minimum: int) -> tuple[list[str], str]:
    """Split off whatever is complete enough to speak. Returns (chunks, rest).

    Sentences shorter than ``minimum`` are held back and merged into the next
    one rather than becoming a clip of their own.
    """
    chunks, start = [], 0
    for m in _BOUNDARY.finditer(buf):
        if m.end() - start >= minimum:
            chunks.append(buf[start:m.end()].strip())
            start = m.end()
    return chunks, buf[start:]


def _parts(resp) -> list:
    """The parts of a streamed chunk, which can legitimately be empty."""
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return list(getattr(content, "parts", None) or []) if content else []


class Brain:
    def __init__(self) -> None:
        if not config.api_key:
            raise RuntimeError(
                "No Gemini API key. Set GEMINI_API_KEY or put 'gemini_api_key' "
                "in config.json. Get a free key at https://aistudio.google.com/apikey"
            )
        self.client = genai.Client(api_key=config.api_key)
        self.model = config["gemini_model"]
        self.history: list[types.Content] = []  # rolling conversation memory
        self._tools = [types.Tool(function_declarations=FUNCTION_DECLARATIONS)]

    @property
    def _config(self) -> types.GenerateContentConfig:
        """Built per request, so a fact remembered a moment ago is already in
        the prompt on the very next turn - and so the clock in it is the real
        one, which is what makes "remind me at six" land at six."""
        now = datetime.now().strftime("%A %d %B %Y, %H:%M")
        return types.GenerateContentConfig(
            system_instruction=(SYSTEM_PROMPT + memory.as_prompt()
                                + f"\n\nThe current local time is {now}."),
            max_output_tokens=config["max_tokens"],
            tools=self._tools,
            # We run the tool loop ourselves so we can execute real actions.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

    def ask(self, user_text: str) -> str:
        """Run one turn. Executes any tool calls and returns the spoken reply.

        Costs a single API request: Gemini either answers, or returns tool
        calls which we execute and confirm using the actions' own messages -
        we don't make a second request just to phrase a confirmation.
        """
        self.history.append(
            types.Content(role="user", parts=[types.Part(text=user_text)])
        )
        self.history = self.history[-24:]  # keep memory bounded

        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=self.history, config=self._config
            )
        except genai_errors.ClientError as exc:
            self.history.pop()  # don't keep the un-answered turn
            return self._client_error(exc)
        except Exception as exc:
            self.history.pop()
            return f"I'm sorry, sir, I ran into a problem: {exc}"

        content = resp.candidates[0].content
        self.history.append(content)
        parts = content.parts or []

        text = " ".join(p.text.strip() for p in parts if getattr(p, "text", None))
        calls = [p.function_call for p in parts if p.function_call]

        if not calls:
            return text.strip() or "Done."

        spoken = self._run_calls(calls)
        reply = (text + " " + " ".join(spoken)).strip() if text else " ".join(spoken)
        self._remember(reply)
        return reply or "Done."

    def ask_stream(self, user_text: str) -> Iterator[str]:
        """Run one turn, handing back each sentence the moment it is written.

        Still a single API request. The point is the ordering: the caller can
        be speaking the first sentence while Gemini is composing the second,
        and any acknowledgement ("Right away, sir.") is out loud before the
        tool it refers to has even run.
        """
        self.history.append(
            types.Content(role="user", parts=[types.Part(text=user_text)])
        )
        self.history = self.history[-24:]

        said: list[str] = []
        calls: list = []
        buf = ""
        spoke = False  # once the first sentence is out, batch the rest
        try:
            stream = self.client.models.generate_content_stream(
                model=self.model, contents=self.history, config=self._config
            )
            for resp in stream:
                for part in _parts(resp):
                    if getattr(part, "text", None):
                        said.append(part.text)
                        buf += part.text
                        ready, buf = _take_sentences(
                            buf, CHUNK_CHARS if spoke else FIRST_CHUNK_CHARS
                        )
                        for chunk in ready:
                            spoke = True
                            yield chunk
                    if getattr(part, "function_call", None):
                        calls.append(part.function_call)
        except genai_errors.ClientError as exc:
            self.history.pop()
            yield self._client_error(exc)
            return
        except Exception as exc:
            self.history.pop()
            yield f"I'm sorry, sir, I ran into a problem: {exc}"
            return

        tail = buf.strip()
        if tail:
            yield tail

        text = "".join(said).strip()
        parts = ([types.Part(text=text)] if text else []) + [
            types.Part(function_call=fc) for fc in calls
        ]
        self.history.append(types.Content(role="model", parts=parts))

        if not calls:
            if not text:
                yield "Done."
            return

        spoken = self._run_calls(calls)
        for line in spoken:
            yield line
        reply = (text + " " + " ".join(spoken)).strip() if text else " ".join(spoken)
        self._remember(reply)

    # ----- shared bookkeeping -------------------------------------------
    @staticmethod
    def _client_error(exc) -> str:
        if getattr(exc, "code", None) == 429:
            return ("I've reached my daily request limit on the free tier, "
                    "sir. It resets shortly.")
        return f"I'm sorry, sir, the service returned an error: {exc.code}."

    def _run_calls(self, calls) -> list[str]:
        """Execute the tool calls and record their results in history."""
        results, spoken = [], []
        for fc in calls:
            args = dict(fc.args) if fc.args else {}
            try:
                out = DISPATCH[fc.name](args)
            except Exception as exc:
                out = f"Action failed: {exc}"
            spoken.append(out)
            results.append(
                types.Part.from_function_response(
                    name=fc.name, response={"result": out}
                )
            )
        self.history.append(types.Content(role="user", parts=results))
        return spoken

    def _remember(self, reply: str) -> None:
        """Record what was actually said, so a follow-up has the context -
        without spending a second request just to phrase a confirmation."""
        self.history.append(
            types.Content(role="model", parts=[types.Part(text=reply)])
        )
