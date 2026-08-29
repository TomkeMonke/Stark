"""Voice I/O for Stark.

- Wake-word + speech-to-text via Vosk (offline). The small English model is
  downloaded automatically on first run.
- Text-to-speech: edge-tts (free British neural voice) or ElevenLabs (paid),
  with pyttsx3 (Windows SAPI5, offline) as the always-available fallback.

Two things here are worth knowing before changing anything:

*Speech is streamed.* A long reply is split into sentences; the next sentence is
synthesized while the current one plays, so Stark starts talking about a second
sooner than he would if he waited for the whole thing.

*Stark listens while he talks.* Playback runs the microphone through a
grammar-restricted recogniser looking for "stop" and friends, so he can be cut
off mid-sentence. Any barge word that appears in what he is currently saying is
struck from the list - otherwise the speakers would interrupt him for you.
"""
from __future__ import annotations

import array
import json
import os
import queue
import re
import tempfile
import threading
import time
import zipfile
from collections import deque
from urllib.request import urlopen

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")  # silence pygame banner

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

from config import config, VOSK_MODEL_DIR, VOSK_MODEL_URL, MODELS_DIR

SetLogLevel(-1)  # silence Vosk's verbose logging
SAMPLE_RATE = 16000
BLOCK = 8000

# Don't act on a barge word in the first moments of a clip: that's usually the
# tail of the previous one still echoing back off the speakers.
BARGE_GRACE_SEC = 0.35
# How much audio from before Vosk noticed speech to keep for Whisper.
LEAD_IN_SEC = 1.5
LEAD_IN_BYTES = int(SAMPLE_RATE * 2 * LEAD_IN_SEC)  # 16-bit mono
# The HUD's voiceprint wants a loudness sample per drawn frame, but a block
# is half a second of audio. Each block is cut into this many slices, which
# at 60fps is one slice per frame - so the envelope keeps the shape of what
# was actually said instead of stepping twice a second.
METER_SUBS = 30
METER_FULL = 9000.0  # int16 peak counted as loud; the HUD auto-gains anyway

_WORDS = re.compile(r"[a-z']+")


def _unlink(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


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
        # Loudness envelope for the HUD. Filled by the audio callback, drained
        # a sample at a time. Capped, so a HUD that has been hidden a while
        # can't come back and replay a backlog of stale audio.
        self._env: deque = deque(maxlen=METER_SUBS + 15)
        self._env_last = 0.0
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
        self._build_barge_matching()

        # Whisper transcribes commands; it loads in the background so startup
        # isn't held up, and the first command waits for it if it has to.
        self._whisper = None
        self._whisper_ready = threading.Event()
        if config["stt_engine"] == "whisper":
            threading.Thread(target=self._load_whisper, daemon=True).start()
        else:
            self._whisper_ready.set()

        # Lazy TTS init so a missing voice doesn't block startup.
        self._tts = None

    def _load_whisper(self) -> None:
        try:
            import warnings

            with warnings.catch_warnings():
                # ctranslate2 4.6 still imports pkg_resources and says so loudly;
                # stark.log is the only diagnostic when running windowless, so
                # keep it for things that matter.
                warnings.filterwarnings("ignore", message=".*pkg_resources.*")
                from faster_whisper import WhisperModel

            self._whisper = WhisperModel(
                config["whisper_model"],
                device=config["whisper_device"],
                compute_type=config["whisper_compute"],
            )
            print(f"[voice] whisper {config['whisper_model']} ready")
        except Exception as exc:
            # Not fatal: Vosk is already transcribing for the endpointing, so
            # Stark falls back to its transcript rather than going deaf.
            print(f"[voice] whisper unavailable ({exc}); using Vosk for commands.")
            self._whisper = None
        finally:
            self._whisper_ready.set()

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
        self._wake_prefix = grammar_words | self._trigger_tokens
        # "[unk]" lets out-of-grammar speech be recognised as unknown.
        self._wake_grammar = json.dumps(sorted(grammar_words) + ["[unk]"])

    def _build_barge_matching(self) -> None:
        words: set[str] = set()
        for phrase in config.get("barge_words", []):
            words.update(phrase.lower().split())
        self._barge_tokens = words
        self._barge_grammar = json.dumps(sorted(words) + ["[unk]"]) if words else ""

    def _grammar_rec(self, grammar: str) -> KaldiRecognizer:
        try:
            return KaldiRecognizer(self._model, SAMPLE_RATE, grammar)
        except Exception as exc:  # word missing from the model's vocabulary, etc.
            print(f"[voice] grammar unavailable ({exc}); using full model.")
            return KaldiRecognizer(self._model, SAMPLE_RATE)

    def _make_wake_rec(self) -> KaldiRecognizer:
        return self._grammar_rec(self._wake_grammar)

    def _is_wake(self, text: str) -> bool:
        return any(tok in self._trigger_tokens for tok in text.lower().split())

    def _strip_wake_prefix(self, text: str) -> str:
        """Drop a leading "hey stark" from a command.

        The wake recogniser hands over the instant it hears the name, so the
        command recogniser often catches it again - and in a follow-up the user
        may say it out of habit. Either way the brain shouldn't see it.

        Whisper punctuates ("Stark, open Notepad."), so strip that before
        matching or the leading word never compares equal.
        """
        words = text.split()
        i = 0
        while i < len(words) and words[i].lower().strip(",.!?;:") in self._wake_prefix:
            i += 1
        return " ".join(words[i:]) if i < len(words) else text

    # ----- audio plumbing -----------------------------------------------
    def _on_audio(self, indata, frames, time_info, status):  # noqa: D401
        data = bytes(indata)
        self._q.put(data)
        self._meter(data)

    def _meter(self, data: bytes) -> None:
        """Chop one block into a loudness envelope for the HUD.

        This runs on the audio driver's own callback thread, so anything that
        goes wrong here is swallowed: a decorative ring is never worth taking
        the microphone down for.
        """
        try:
            samples = array.array("h")
            samples.frombytes(data[:len(data) // 2 * 2])
            step = len(samples) // METER_SUBS
            if step < 1:
                return
            for i in range(METER_SUBS):
                chunk = samples[i * step:(i + 1) * step]
                peak = max(max(chunk), -min(chunk))
                self._env.append(min(1.0, peak / METER_FULL))
        except Exception:
            pass

    def next_level(self) -> float:
        """One 0..1 loudness sample for the HUD, oldest first.

        Audio arrives in half-second blocks and the HUD asks sixty times a
        second, so the envelope is played back rather than sampled. That puts
        it half a second behind, which doesn't read as lag on a ring that is a
        picture of what was just said in the first place.
        """
        if self._env:
            self._env_last = self._env.popleft()
        else:
            self._env_last *= 0.75  # starved: decay rather than snap to zero
        return self._env_last

    def _drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    # ----- listening -----------------------------------------------------
    def wait_for_wake(self, is_paused=None, wake_event=None,
                      announce_event=None) -> str:
        """Block until there is a reason to stop waiting. Returns why.

        Normally that is a wake word, and the matched text comes back. The two
        events are the other ways out: ``wake_event`` is the push-to-talk
        hotkey ("hotkey"), and ``announce_event`` is Stark having something to
        say of his own, a timer going off ("announce").

        While ``is_paused()`` returns True the mic is drained but never
        triggers, so Stark stays dormant until listening is resumed. Both
        events are honoured anyway: pausing silences the microphone, but a
        keypress is unambiguous, and a timer the user set themselves should
        still go off.
        """
        self._rec = self._make_wake_rec()
        while True:
            if wake_event is not None and wake_event.is_set():
                wake_event.clear()
                self._drain()
                return "hotkey"
            if announce_event is not None and announce_event.is_set():
                return "announce"
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

    def listen_command(self, open_window_sec: float | None = None,
                       on_speech=None) -> str:
        """Capture one spoken command. Returns "" if nothing was said.

        ``open_window_sec`` is how long to wait for the user to *start*
        speaking. After the wake word that's short, because they are already
        mid-sentence; for a follow-up it's the whole grace window. Once speech
        starts, the command ends on trailing silence or the length cap,
        whichever comes first.

        ``on_speech`` fires the moment the first word lands, which is what the
        HUD uses to drop its follow-up countdown.

        Vosk runs throughout to decide *when* the command starts and stops. The
        raw audio is kept as it goes by, so Whisper can then say what it was.
        """
        self._drain()
        self._rec = KaldiRecognizer(self._model, SAMPLE_RATE)
        timeout = config["command_timeout_sec"]
        silence = config["command_silence_sec"]
        window = timeout if open_window_sec is None else open_window_sec

        start = time.monotonic()
        speech_start = None
        last_change = start
        last_partial = ""
        captured: list[bytes] = []
        buffered = 0  # bytes currently held in `captured`

        def finish(vosk_text: str) -> str:
            """Whisper's reading of the audio, or Vosk's if it has none."""
            heard = self._transcribe(b"".join(captured)) or vosk_text
            return self._strip_wake_prefix(heard) if heard else ""

        while True:
            try:
                data = self._q.get(timeout=0.1)
            except queue.Empty:
                data = None
            now = time.monotonic()

            if data is not None:
                captured.append(data)
                buffered += len(data)
                if self._rec.AcceptWaveform(data):
                    final = json.loads(self._rec.Result()).get("text", "").strip()
                    if final:  # Vosk found the end of the utterance itself
                        return finish(final)
                    last_partial = ""
                else:
                    partial = json.loads(
                        self._rec.PartialResult()).get("partial", "").strip()
                    # Vosk repeats the same partial through a silence, so only a
                    # *change* counts as the user still talking.
                    if partial and partial != last_partial:
                        last_partial = partial
                        last_change = now
                        if speech_start is None:
                            speech_start = now
                            if on_speech is not None:
                                on_speech()

            if speech_start is None:
                if now - start > window:
                    return ""
                # Don't hand Whisper a whole window of silence, but do keep the
                # last second and a half of it. Vosk reports a word only once it
                # is confident, by which time the opening syllable is well past
                # - trim any tighter and "open Chrome" arrives as "Chrome".
                while buffered > LEAD_IN_BYTES and len(captured) > 1:
                    buffered -= len(captured.pop(0))
                continue

            if now - last_change > silence or now - speech_start > timeout:
                return finish(json.loads(self._rec.FinalResult()).get("text", "").strip())

    def _transcribe(self, raw: bytes) -> str:
        """Whisper's transcript of one utterance, or "" to fall back to Vosk."""
        if not raw or config["stt_engine"] != "whisper":
            return ""
        # Only the first command can ever wait here, and only if it arrives
        # before the model has finished loading.
        self._whisper_ready.wait(timeout=30)
        if self._whisper is None:
            return ""
        try:
            import numpy as np

            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = self._whisper.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False,
            )
            return " ".join(s.text for s in segments).strip()
        except Exception as exc:
            print(f"[voice] whisper failed ({exc}); using Vosk's transcript.")
            return ""

    # ----- speaking ------------------------------------------------------
    def speak(self, text) -> bool:
        """Say something. Returns True if the user cut in before it finished.

        ``text`` is either a finished string or an iterator of chunks - the
        brain handing over each sentence as it writes it. The streamed form is
        where the latency goes: synthesis of sentence one starts while the
        model is still working on sentence two. A string that is already
        complete is spoken as a single clip, since splitting it up would only
        add the silence the voice service pads each clip with.
        """
        streamed = not isinstance(text, str)
        if not streamed and not text:
            return False
        # Drain the mic so Stark doesn't transcribe the tail of his last clip.
        self._drain()
        engine = config["tts_engine"]
        interrupted = False

        if engine in ("edge", "elevenlabs"):
            chunks = text if streamed else [text]
            interrupted = self._speak_streamed(chunks, engine, config["barge_in"])
        else:  # SAPI: synchronous and uninterruptible, but always available
            self._speak_sapi(text if not streamed else " ".join(text))

        self._drain()
        return interrupted

    def _speak_streamed(self, chunks, engine: str, barge_enabled: bool) -> bool:
        """Play each chunk while the next one synthesizes.

        ``chunks`` is consumed on the producer thread, so when it's the brain's
        generator, Gemini is still writing while Stark is already talking.
        """
        synth = self._synth_edge if engine == "edge" else self._synth_elevenlabs
        clips: queue.Queue = queue.Queue(maxsize=1)
        cancel = threading.Event()

        def produce() -> None:
            try:
                for i, chunk in enumerate(chunks):
                    if cancel.is_set():
                        break
                    path = os.path.join(tempfile.gettempdir(), f"stark_tts_{i}.mp3")
                    clips.put((chunk, path if synth(chunk, path) else None))
            except Exception as exc:
                print(f"[voice] the reply stopped short ({exc})")
            finally:
                clips.put(None)  # end marker, whatever happened

        threading.Thread(target=produce, daemon=True).start()

        interrupted = False
        while True:
            item = clips.get()
            if item is None:
                break
            chunk, path = item
            if interrupted:  # keep draining so the producer can finish and exit
                _unlink(path)
                continue
            if path is None:  # the neural engine failed; say this bit offline
                self._speak_sapi(chunk)
                continue
            # Build the barge list per clip: only the words in *this* clip need
            # suppressing, and with a stream that's all we know anyway.
            barge = self._make_barge(chunk) if barge_enabled else None
            interrupted = self._play_mp3(path, barge=barge)
            _unlink(path)
            if interrupted:
                cancel.set()
        return interrupted

    # ----- barge-in ------------------------------------------------------
    def _make_barge(self, spoken_text: str):
        """A recogniser plus the words allowed to interrupt this reply.

        Everything Stark is about to say is struck off the list, so his own
        voice coming back through the speakers can never stop him.
        """
        if not self._barge_grammar:
            return None
        his_words = set(_WORDS.findall(spoken_text.lower()))
        tokens = self._barge_tokens - his_words
        if not tokens:
            return None
        return self._grammar_rec(self._barge_grammar), tokens

    def _barge_heard(self, barge) -> bool:
        """Feed everything the mic has captured so far to the barge recogniser."""
        rec, tokens = barge
        heard = False
        while True:
            try:
                data = self._q.get_nowait()
            except queue.Empty:
                return heard
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "")
            else:
                text = json.loads(rec.PartialResult()).get("partial", "")
            if any(tok in tokens for tok in text.split()):
                heard = True

    def _ensure_mixer(self):
        import pygame

        if not getattr(self, "_mixer_ready", False):
            pygame.mixer.init()
            self._mixer_ready = True
        return pygame

    def _play_mp3(self, path: str, barge=None) -> bool:
        """Play a clip. Returns True if a barge word cut it short."""
        pygame = self._ensure_mixer()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        start = time.monotonic()
        interrupted = False
        while pygame.mixer.music.get_busy():
            if barge is None:
                self._drain()
            elif self._barge_heard(barge) and time.monotonic() - start > BARGE_GRACE_SEC:
                interrupted = True
                break
            time.sleep(0.02)
        pygame.mixer.music.stop()
        pygame.mixer.music.unload()
        return interrupted

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

    # ----- synthesis backends -------------------------------------------
    def _synth_edge(self, text: str, path: str) -> bool:
        try:
            import asyncio
            import edge_tts

            async def synth():
                comm = edge_tts.Communicate(
                    text,
                    config["edge_voice"],
                    rate=config["edge_rate"],
                    pitch=config["edge_pitch"],
                )
                await comm.save(path)

            asyncio.run(synth())
            return True
        except Exception as exc:
            print(f"[voice] edge-tts failed ({exc}); using offline voice.")
            return False

    def _synth_elevenlabs(self, text: str, path: str) -> bool:
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
            with open(path, "wb") as f:
                f.write(r.content)
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
