"""Stark configuration: paths, settings, and secret loading.

Settings live in config.json next to this file. The Gemini API key is read from
(in order): GEMINI_API_KEY, GOOGLE_API_KEY env vars, then config.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
MODELS_DIR = BASE_DIR / "models"

# Vosk small English model - downloaded automatically on first run.
VOSK_MODEL_NAME = "vosk-model-small-en-us-0.15"
VOSK_MODEL_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
)
VOSK_MODEL_DIR = MODELS_DIR / VOSK_MODEL_NAME

DEFAULTS = {
    # Brain - Google Gemini (free tier via https://aistudio.google.com/apikey)
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash-lite",  # much higher free-tier daily quota
    "max_tokens": 1024,
    # Wake word - any of these phrases activates Stark.
    "wake_words": ["stark", "hey stark", "hi stark", "okay stark"],
    # Extra words that count as the wake word (common Vosk mishearings of
    # "stark"). Add more if Stark keeps mishearing you; remove ones that cause
    # false triggers.
    "wake_extra": ["starks", "stark's", "stork", "sark", "starc"],
    "paused": False,           # start dormant (ignore the mic) until resumed
    "wake_chime": True,        # soft ping when the wake word is detected
    "chime_volume": 0.12,      # 0.0 (silent) .. 1.0 (full)
    # Voice engine: "edge" (free neural), "elevenlabs" (paid), "sapi" (offline)
    "tts_engine": "edge",
    # edge-tts - free Microsoft neural voices (British Jarvis = en-GB-RyanNeural)
    "edge_voice": "en-GB-RyanNeural",
    "edge_rate": "+0%",
    "edge_pitch": "+0Hz",
    # ElevenLabs - paid; set a key (here or ELEVENLABS_API_KEY) and tts_engine
    # to "elevenlabs" to enable. Default voice is "George" (British male).
    "elevenlabs_api_key": "",
    "elevenlabs_voice_id": "JBFqnCBsd6RMkjVDRZzb",
    "elevenlabs_model": "eleven_turbo_v2_5",
    # Offline fallback (pyttsx3 / SAPI5)
    "tts_rate": 180,
    "tts_voice_hint": "",  # substring to match a preferred SAPI voice name
    # Listening behaviour
    "command_timeout_sec": 8.0,    # max length of a single spoken command
    "command_silence_sec": 1.0,    # silence that ends a command
    # Follow-ups: after Stark answers, the mic stays open briefly so the next
    # command needs no wake word. Set the window to 0 to turn this off.
    "followup_enabled": True,
    "followup_window_sec": 7.0,
    # Barge-in: cut Stark off mid-sentence by saying one of these. Words that
    # appear in what he is currently saying are ignored, so he can't interrupt
    # himself. Needs a neural voice (edge/elevenlabs); the offline SAPI
    # fallback can't be stopped part-way.
    "barge_in": True,
    "barge_words": ["stop", "stark", "cancel", "quiet", "enough", "wait"],
    # Start speaking the first sentence while the model is still writing the
    # rest, instead of waiting for the whole reply. Turn it off to go back to
    # one request, one finished answer, one clip.
    "stream_speech": True,
    # Push-to-talk: wake Stark without saying his name. Works while paused.
    "hotkey_enabled": True,
    "hotkey": "ctrl+alt+s",
    # HUD
    "hud_size": 460,
    # Quick Control - the pop-up settings panel Stark drives for screen and
    # sound. Empty dir means "look next to Stark, then in the home folder".
    "quickcontrol_dir": "",
    "quickcontrol_autostart": True,  # start the panel if a command needs it
}


class Config:
    def __init__(self) -> None:
        self.data = dict(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                self.data.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception as exc:  # pragma: no cover
                print(f"[config] could not read config.json: {exc}")
        MODELS_DIR.mkdir(parents=True, exist_ok=True)

    def __getitem__(self, key: str):
        return self.data.get(key, DEFAULTS.get(key))

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    @property
    def api_key(self) -> str:
        return (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or self.data.get("gemini_api_key", "")
        )

    @property
    def elevenlabs_key(self) -> str:
        return os.environ.get("ELEVENLABS_API_KEY") or self.data.get(
            "elevenlabs_api_key", ""
        )

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


config = Config()
