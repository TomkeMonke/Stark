"""The questions Stark can't answer from the conversation alone.

Each goes back to Gemini with something extra attached - a picture of the
screen, Google Search grounding, or whatever the user has copied - and each
returns a finished sentence for Stark to say out loud, the same contract every
other action has.

Each of these costs a *second* API request for that command, unlike everything
else Stark does. That is the price of the image and of grounded search, and it
is why they are separate tools the model has to choose rather than something
bolted onto every turn.
"""
from __future__ import annotations

import io

from google.genai import types

from config import config

# Replies are spoken, so they must sound like speech, not like a web page.
SPOKEN_STYLE = (
    "You are Stark, a calm, dry-witted assistant speaking out loud to the user. "
    "Answer in one or two short sentences. Never use markdown, bullet points, "
    "emoji, headings or code blocks - everything you write is read aloud by a "
    "text-to-speech voice. Give the answer itself, not a description of where "
    "to find it."
)

# The screen goes to the model as a JPEG. Full resolution is a lot of tokens for
# no benefit; this is still comfortably readable for text on screen.
MAX_SCREEN_PX = 1600
JPEG_QUALITY = 70

_client = None


def _gemini():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=config.api_key)
    return _client


def _spoken(resp) -> str:
    text = (getattr(resp, "text", "") or "").strip()
    return " ".join(text.split())


def look_at_screen(question: str) -> str:
    """Answer a question about whatever is on screen right now."""
    try:
        import pyautogui

        shot = pyautogui.screenshot()
        shot.thumbnail((MAX_SCREEN_PX, MAX_SCREEN_PX))
        buf = io.BytesIO()
        shot.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    except Exception as exc:
        return f"I couldn't capture the screen, sir. {exc}"

    ask = question.strip() or "What is on the screen?"
    try:
        resp = _gemini().models.generate_content(
            model=config["vision_model"] or config["gemini_model"],
            contents=[types.Content(role="user", parts=[
                types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
                types.Part(text=ask),
            ])],
            config=types.GenerateContentConfig(
                system_instruction=SPOKEN_STYLE,
                max_output_tokens=config["max_tokens"],
            ),
        )
    except Exception as exc:
        return f"I couldn't make sense of the screen, sir. {_reason(exc)}"
    return _spoken(resp) or "I'm not sure what I'm looking at, sir."


def answer_about_clipboard(question: str) -> str:
    """Do something with what the user copied: translate it, summarize it,
    explain it. Reading it back verbatim needs no model - that's
    clipboard.read_aloud, and it costs nothing."""
    import clipboard

    raw = clipboard.text()
    if not raw.strip():
        return "There's nothing on your clipboard, sir."

    ask = question.strip() or "What is this?"
    try:
        resp = _gemini().models.generate_content(
            # Plain text and no search, so neither of the overrides applies.
            model=config["gemini_model"],
            contents=[types.Content(role="user", parts=[
                types.Part(text="The user has this on their clipboard:\n\n"
                                + raw[:clipboard.MAX_MODEL_CHARS]),
                types.Part(text=ask),
            ])],
            config=types.GenerateContentConfig(
                system_instruction=SPOKEN_STYLE,
                max_output_tokens=config["max_tokens"],
            ),
        )
    except Exception as exc:
        return f"I couldn't work with that, sir. {_reason(exc)}"
    return _spoken(resp) or "I'm not sure what to make of that, sir."


def answer_from_web(question: str) -> str:
    """Answer from a live Google Search, for anything current."""
    ask = question.strip()
    if not ask:
        return "What would you like me to look up, sir?"
    try:
        resp = _gemini().models.generate_content(
            model=config["search_model"] or config["gemini_model"],
            contents=ask,
            config=types.GenerateContentConfig(
                system_instruction=SPOKEN_STYLE,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                max_output_tokens=config["max_tokens"],
            ),
        )
    except Exception as exc:
        return f"I couldn't reach the web just now, sir. {_reason(exc)}"
    return _spoken(resp) or f"I found nothing useful about {ask}, sir."


def _reason(exc) -> str:
    code = getattr(exc, "code", None)
    if code == 429:
        return "I've hit my request limit for now."
    return "It gave me an error."
