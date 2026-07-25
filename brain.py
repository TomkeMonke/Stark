"""Stark's brain: Google Gemini decides what to say and which actions to run.

Uses Gemini function calling. Gemini receives the user's spoken command plus a
set of tools (open app, open url, web search, system control, type text). It
either replies conversationally, calls tools, or both. Runs on Gemini's free
tier — get a key at https://aistudio.google.com/apikey
"""
from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import actions
import quick_control
from config import config

SYSTEM_PROMPT = """You are Stark, a personal AI assistant modeled on J.A.R.V.I.S. \
from Iron Man. You run on the user's Windows PC and are spoken to out loud, so \
your replies are read aloud by a text-to-speech voice.

Personality: calm, intelligent, dry wit, unfailingly polite. Address the user as \
"sir" occasionally but not every sentence. Be concise — one or two short sentences \
is ideal, since everything you say is spoken aloud. Never use markdown, bullet \
points, emoji, or code blocks in your spoken replies.

Capabilities: you can open applications and files, open websites, search the web, \
control the system (volume, lock, sleep, screenshots), and type text. You also \
drive the user's Quick Control panel for screen and sound: brightness, warm \
(night) mode, exact or per-app volume, scene presets, keep-awake, and recalling \
saved window arrangements — use the quick_control tool for all of those, and for \
questions about the current settings. Use the provided tools to take action. If \
the user just wants conversation or an answer, simply reply — do not call a tool.

When a tool reports back, the user hears that sentence, so don't repeat it.

Safety: for shutdown or restart, briefly confirm what you're about to do in your \
reply. If a request is ambiguous, ask one short clarifying question instead of \
guessing."""

_STR = types.Schema(type=types.Type.STRING)


def _decl(name, description, properties, required):
    return types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=types.Schema(
            type=types.Type.OBJECT, properties=properties, required=required
        ),
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
    _decl("web_search", "Search the web for a query in the default browser.",
          {"query": _STR}, ["query"]),
    _decl("type_text", "Type the given text into whatever window currently has focus.",
          {"text": _STR}, ["text"]),
    _decl("system_control",
          "Control the system. 'action' is one of: volume_up, volume_down, mute, "
          "lock, sleep, shutdown, restart, cancel_shutdown, screenshot.",
          {"action": types.Schema(
              type=types.Type.STRING,
              enum=["volume_up", "volume_down", "mute", "lock", "sleep",
                    "shutdown", "restart", "cancel_shutdown", "screenshot"])},
          ["action"]),
    _decl("quick_control",
          "Screen, sound and window controls, through the user's Quick Control "
          "panel. Use this for anything about brightness, warm or night mode, "
          "exact volume levels, one app's volume, keeping the PC awake, "
          "arranging windows, or opening the panel itself. "
          "'value' is a 0-100 level — or, with brightness_up / brightness_down / "
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
    "type_text": lambda i: actions.type_text(i["text"]),
    "system_control": lambda i: actions.system_control(i["action"]),
    "quick_control": lambda i: actions.quick_control(
        i["command"], i.get("value"), i.get("target")
    ),
}


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
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=config["max_tokens"],
            tools=[types.Tool(function_declarations=FUNCTION_DECLARATIONS)],
            # We run the tool loop ourselves so we can execute real actions.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

    def ask(self, user_text: str) -> str:
        """Run one turn. Executes any tool calls and returns the spoken reply.

        Costs a single API request: Gemini either answers, or returns tool
        calls which we execute and confirm using the actions' own messages —
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
            if getattr(exc, "code", None) == 429:
                return ("I've reached my daily request limit on the free tier, "
                        "sir. It resets shortly.")
            return f"I'm sorry, sir, the service returned an error: {exc.code}."
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
        # Record the tool results + a synthetic spoken turn so history stays
        # coherent for follow-ups, without a second API call.
        self.history.append(types.Content(role="user", parts=results))
        reply = (text + " " + " ".join(spoken)).strip() if text else " ".join(spoken)
        self.history.append(
            types.Content(role="model", parts=[types.Part(text=reply)])
        )
        return reply or "Done."
