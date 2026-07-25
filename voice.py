"""Voice I/O for Stark.

- Wake-word + speech-to-text via Vosk (offline). The small English model is
  downloaded automatically on first run.
- Text-to-speech: edge-tts (free British neural voice) or ElevenLabs (paid),
  with pyttsx3 (Windows SAPI5, offline) as the always-available fallback.
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import time
import zipfile
from urllib.request import urlopen

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")  # silence pygame banner

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

from config import config, VOSK_MODEL_DIR, VOSK_MODEL_URL, MODELS_DIR

SetLogLevel(-1)  # silence Vosk's verbose logging
SAMPLE_RATE = 16000
BLOCK = 8000


def ensure_model(progress=print) -> None:
    """Download + unzip the Vosk model if it isn't present yet."""
    if VOSK_MODEL_DIR.exists():
        return
    progress("First run: downloading the speech model (~40 MB)...")
    zip_path = MODELS_DIR / "vosk-model.zip"
    with urlopen(VOSK_MODEL_URL) as resp, open(zip_path, "wb") as out:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        last_pct = -10
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            out.write(chunk)
            read += len(chunk)
            if total:
                pct = read * 100 // total
                if pct >= last_pct + 10:
                    progress(f"  downloading... {pct}%")
                    last_pct = pct
    progress("Extracting speech model...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(MODELS_DIR)
    zip_path.unlink(missing_ok=True)
    progress("Speech model ready.")


class VoiceEngine:
    def __init__(self) -> None:
        ensure_model()
        self._model = Model(str(VOSK_MODEL_DIR))
        self._rec = KaldiRecognizer(self._model, SAMPLE_RATE)
        self._q: queue.Queue = queue.Queue()
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK,
            dtype="int16",
            channels=1,
            callback=self._on_audio,
        )
        self._stream.start()

        self.wake_words = [w.lower() for w in config["wake_words"]]
        self._build_wake_matching()

        # Lazy TTS init so a missing voice doesn't block startup.
        self._tts = None

    # ----- wake-word setup ----------------------------------------------
    def _build_wake_matching(self) -> None:
        """Prepare a restricted grammar + the set of whole words that trigger.

        Restricting the recogniser to just the wake phrases makes Vosk far more
        accurate on them and lets it tag unrelated speech as unknown, so we get
        fewer misses and fewer false matches than free-form transcription.
        """
        extra = [w.lower() for w in config.get("wake_extra", [])]
        # Trigger on the key word of each phrase (e.g. "stark") plus mishearings.
        self._trigger_tokens = {w.split()[-1] for w in self.wake_words} | set(extra)

        grammar_words = set()
        for phrase in self.wake_words:
            grammar_words.update(phrase.split())
        grammar_words.update(extra)
        # "[unk]" lets out-of-grammar speech be recognised as unknown.
        self._wake_grammar = json.dumps(sorted(grammar_words) + ["[unk]"])

    def _make_wake_rec(self) -> KaldiRecognizer:
        try:
            rec = KaldiRecognizer(self._model, SAMPLE_RATE, self._wake_grammar)
        except Exception as exc:  # word missing from model vocab, etc.
            print(f"[voice] wake grammar unavailable ({exc}); using full model.")
            rec = KaldiRecognizer(self._model, SAMPLE_RATE)
        return rec

    def _is_wake(self, text: str) -> bool:
        return any(tok in self._trigger_tokens for tok in text.lower().split())

    # ----- audio plumbing -----------------------------------------------
    def _on_audio(self, indata, frames, time_info, status):  # noqa: D401
        self._q.put(bytes(indata))

    def _drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    # ----- listening -----------------------------------------------------
    def wait_for_wake(self, is_paused=None) -> str:
        """Block until a wake word is heard. Returns the matched text.

        While ``is_paused()`` returns True the mic is drained but never
        triggers, so Stark stays dormant until listening is resumed.
        """
        self._rec = self._make_wake_rec()
        while True:
            try:
                data = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if is_paused is not None and is_paused():
                continue  # dormant: drop audio without matching
            if self._rec.AcceptWaveform(data):
                text = json.loads(self._rec.Result()).get("text", "")
                if self._is_wake(text):
                    return text
            else:
                partial = json.loads(self._rec.PartialResult()).get("partial", "")
                if self._is_wake(partial):
                    self._rec.Result()  # flush so the next call starts clean
                    return partial

    def listen_command(self) -> str:
        """Capture one spoken command after the wake word."""
        self._drain()
        self._rec = KaldiRecognizer(self._model, SAMPLE_RATE)
        start = time.monotonic()
        last_speech = start
        timeout = config["command_timeout_sec"]
        silence = config["command_silence_sec"]

        while True:
            data = self._q.get()
            now = time.monotonic()
            if self._rec.AcceptWaveform(data):
                final = json.loads(self._rec.Result()).get("text", "").strip()
                if final:
                    return final
            else:
                partial = json.loads(self._rec.PartialResult()).get("partial", "").strip()
                if partial:
                    last_speech = now

            # End on trailing silence (if we already heard something) or timeout.
            if now - last_speech > silence and now - start > 1.5:
                final = json.loads(self._rec.FinalResult()).get("text", "").strip()
                if final:
                    return final
            if now - start > timeout:
                return json.loads(self._rec.FinalResult()).get("text", "").strip()

    # ----- speaking ------------------------------------------------------
    def speak(self, text: str) -> None:
        if not text:
            return
        # Drain mic so Stark doesn't transcribe his own voice.
        self._drain()
        engine = config["tts_engine"]
        spoken = False
        if engine == "elevenlabs":
            spoken = self._speak_elevenlabs(text)
        elif engine == "edge":
            spoken = self._speak_edge(text)
        if not spoken:  # always-available offline fallback
            self._speak_sapi(text)
        self._drain()

    def _ensure_mixer(self):
        import pygame

        if not getattr(self, "_mixer_ready", False):
            pygame.mixer.init()
            self._mixer_ready = True
        return pygame

    def _play_mp3(self, path: str) -> None:
        pygame = self._ensure_mixer()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        pygame.mixer.music.unload()

    @staticmethod
    def _make_chime(path) -> None:
        """Synthesize a short two-note ascending 'ping' to a WAV file."""
        import array
        import math
        import wave

        sr = 44100

        def tone(freq, dur):
            n = int(sr * dur)
            return [math.sin(2 * math.pi * freq * i / sr) * math.exp(-3.5 * i / n)
                    for i in range(n)]

        sig = tone(784, 0.10) + tone(1175, 0.20)  # G5 -> D6
        data = array.array(
            "h", [int(max(-1.0, min(1.0, s)) * 0.6 * 32767) for s in sig]
        )
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(data.tobytes())

    def chime(self) -> None:
        """Play the wake-confirmation ping (blocks ~0.3s so the mic ignores it)."""
        if not config.get("wake_chime", True):
            return
        try:
            path = MODELS_DIR / "chime.wav"
            if not path.exists():
                self._make_chime(path)
            pygame = self._ensure_mixer()
            snd = pygame.mixer.Sound(str(path))
            snd.set_volume(float(config.get("chime_volume", 0.22)))
            ch = snd.play()
            while ch and ch.get_busy():
                time.sleep(0.02)
        except Exception as exc:
            print(f"[voice] chime failed ({exc})")

    def _speak_edge(self, text: str) -> bool:
        try:
            import asyncio
            import edge_tts

            path = os.path.join(tempfile.gettempdir(), "stark_tts.mp3")

            async def synth():
                comm = edge_tts.Communicate(
                    text,
                    config["edge_voice"],
                    rate=config["edge_rate"],
                    pitch=config["edge_pitch"],
                )
                await comm.save(path)

            asyncio.run(synth())
            self._play_mp3(path)
            return True
        except Exception as exc:
            print(f"[voice] edge-tts failed ({exc}); using offline voice.")
            return False

    def _speak_elevenlabs(self, text: str) -> bool:
        key = config.elevenlabs_key
        voice_id = config["elevenlabs_voice_id"]
        if not key or not voice_id:
            print("[voice] ElevenLabs not configured; using offline voice.")
            return False
        try:
            import requests

            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            r = requests.post(
                url,
                headers={"xi-api-key": key, "accept": "audio/mpeg",
                         "content-type": "application/json"},
                json={"text": text, "model_id": config["elevenlabs_model"]},
                timeout=30,
            )
            r.raise_for_status()
            path = os.path.join(tempfile.gettempdir(), "stark_tts.mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            self._play_mp3(path)
            return True
        except Exception as exc:
            print(f"[voice] ElevenLabs failed ({exc}); using offline voice.")
            return False

    def _speak_sapi(self, text: str) -> None:
        if self._tts is None:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", config["tts_rate"])
            hint = (config["tts_voice_hint"] or "").lower()
            if hint:
                for v in engine.getProperty("voices"):
                    if hint in v.name.lower():
                        engine.setProperty("voice", v.id)
                        break
            self._tts = engine
        self._tts.say(text)
        self._tts.runAndWait()

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
