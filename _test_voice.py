"""Offline checks for the conversational voice loop.

Everything here runs without a microphone and without a Gemini key: test speech
is synthesized with the same TTS engine Stark speaks with, decoded to the raw
16 kHz mono PCM the recogniser expects, and pushed straight into the audio queue
in place of the mic. That makes the wake/command/barge paths testable for real
rather than by inspection.

Run:  .venv\\Scripts\\python.exe _test_voice.py
"""
from __future__ import annotations

import asyncio
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from config import config
from voice import SAMPLE_RATE, VoiceEngine

BLOCK_BYTES = 8000  # bytes per queued chunk, matching the real capture stream
_cache: dict[str, bytes] = {}

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# ----- synthesizing test speech -----------------------------------------
def pcm(text: str) -> bytes:
    """Speak `text` with edge-tts and return it as 16 kHz mono PCM."""
    if text in _cache:
        return _cache[text]
    import edge_tts

    mp3 = os.path.join(tempfile.gettempdir(), "stark_test_in.mp3")

    async def synth():
        await edge_tts.Communicate(text, config["edge_voice"]).save(mp3)

    asyncio.run(synth())
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-i", mp3,
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    os.unlink(mp3)
    _cache[text] = raw
    return raw


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def feed(engine: VoiceEngine, data: bytes) -> None:
    """Push audio into the engine's queue the way the mic callback would."""
    for i in range(0, len(data), BLOCK_BYTES):
        engine._q.put(data[i:i + BLOCK_BYTES])


def feed_live(engine: VoiceEngine, data: bytes, delay: float = 0.2):
    """Same, but paced in real time from a thread, so the timing assertions
    mean something - and started late enough that listen_command's opening
    drain (which exists to discard Stark's own voice) doesn't eat it."""
    seconds_per_block = BLOCK_BYTES / (SAMPLE_RATE * 2)

    def run() -> None:
        time.sleep(delay)
        for i in range(0, len(data), BLOCK_BYTES):
            engine._q.put(data[i:i + BLOCK_BYTES])
            time.sleep(seconds_per_block)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# ----- pure logic --------------------------------------------------------
def test_hotkey_parsing() -> None:
    print("\nhotkey parsing")
    import hotkey

    check("ctrl+alt+s parses", hotkey.parse("ctrl+alt+s") == (0x0002 | 0x0001, 0x53),
          hotkey.parse("ctrl+alt+s"))
    check("modifier order doesn't matter",
          hotkey.parse("alt+ctrl+s") == hotkey.parse("ctrl+alt+s"))
    check("function keys parse", hotkey.parse("ctrl+f9") == (0x0002, 0x78),
          hotkey.parse("ctrl+f9"))
    check("a spec with no key is rejected", hotkey.parse("ctrl+alt") is None)
    check("junk is rejected", hotkey.parse("") is None)
    check("pretty prints for menus", hotkey.pretty("ctrl+alt+s") == "Ctrl+Alt+S",
          hotkey.pretty("ctrl+alt+s"))


# ----- recogniser-backed -------------------------------------------------
def test_wake_prefix(engine: VoiceEngine) -> None:
    print("\nwake-word stripping")
    check("'hey stark open chrome' -> 'open chrome'",
          engine._strip_wake_prefix("hey stark open chrome") == "open chrome",
          engine._strip_wake_prefix("hey stark open chrome"))
    check("a bare name is left alone rather than emptied",
          engine._strip_wake_prefix("stark") == "stark")
    check("an ordinary command is untouched",
          engine._strip_wake_prefix("what time is it") == "what time is it")


def test_barge_suppression(engine: VoiceEngine) -> None:
    print("\nbarge-in word suppression")
    _, tokens = engine._make_barge("Opening Chrome for you.")
    check("'stop' can interrupt an unrelated reply", "stop" in tokens, tokens)

    made = engine._make_barge("Shall I stop the download, sir?")
    _, tokens = made
    check("Stark can't interrupt himself saying 'stop'", "stop" not in tokens, tokens)
    check("the other barge words still work", "cancel" in tokens, tokens)

    check("a reply containing every barge word disables barge-in entirely",
          engine._make_barge(" ".join(engine._barge_tokens)) is None)


def test_command_capture(engine: VoiceEngine) -> None:
    print("\nlistening (synthetic speech through the recogniser)")
    engine._drain()
    feed_live(engine, pcm("open chrome") + silence(2.0))
    t0 = time.monotonic()
    heard = engine.listen_command()
    took = time.monotonic() - t0
    check("transcribes a command", "chrome" in heard.lower(), repr(heard))
    check("ends on trailing silence, not the 8s cap", took < 5.0, f"{took:.1f}s")

    # A command with the wake word still in front of it. Don't assert on the
    # exact transcript: the small Vosk model is genuinely unreliable on
    # free-form speech (it renders this one as "stop a coat been though pad"
    # about one run in three), which is a known limit, not a regression. The
    # stripping logic is checked exactly, and deterministically, in
    # test_wake_prefix; what's worth checking here is that something comes back
    # and the brain is never handed a wake word.
    engine._drain()
    feed_live(engine, pcm("stark open notepad") + silence(2.0))
    heard = engine.listen_command()
    print(f"    heard: {heard!r}")
    check("a wake-prefixed command still produces a command",
          bool(heard) and not heard.lower().startswith(("hey", "stark")),
          repr(heard))


def test_whisper(engine: VoiceEngine) -> None:
    print("\nwhisper vs vosk on the commands vosk gets wrong")
    engine._whisper_ready.wait(60)
    if engine._whisper is None:
        check("whisper is available", False, "model failed to load")
        return

    # Two phrases the small Vosk model reliably mangles: it hears "said the
    # brightness" and "a range my windows". Whisper gets both right.
    for phrase, want in (("set the brightness to forty", "brightness"),
                         ("arrange my windows for coding", "arrange")):
        config.data["stt_engine"] = "whisper"
        engine._drain()
        feeder = feed_live(engine, pcm(phrase) + silence(2.0))
        heard = engine.listen_command()
        feeder.join()
        print(f"    whisper: {heard!r}")
        check(f"whisper hears {want!r} in {phrase!r}", want in heard.lower(),
              repr(heard))

    # The lead-in buffer matters: Vosk only reports a word once it is sure, so
    # without it Whisper is handed audio that starts mid-word.
    config.data["stt_engine"] = "whisper"
    engine._drain()
    feeder = feed_live(engine, pcm("open chrome") + silence(2.0))
    heard = engine.listen_command()
    feeder.join()
    check("the first word is not clipped off", heard.lower().startswith("open"),
          repr(heard))

    # Turning it off falls back to Vosk rather than going deaf.
    config.data["stt_engine"] = "vosk"
    check("with whisper off, nothing is transcribed by it",
          engine._transcribe(pcm("open chrome")) == "")
    engine._drain()
    feeder = feed_live(engine, pcm("open chrome") + silence(2.0))
    heard = engine.listen_command()
    feeder.join()
    check("and Vosk still answers", "chrome" in heard.lower(), repr(heard))
    config.data["stt_engine"] = "whisper"


def test_followup_window(engine: VoiceEngine) -> None:
    print("\nfollow-up window")
    engine._drain()
    feed_live(engine, silence(4.0))
    t0 = time.monotonic()
    heard = engine.listen_command(open_window_sec=1.5)
    took = time.monotonic() - t0
    check("silence closes the window", heard == "", repr(heard))
    check("and closes it on time", 1.3 < took < 3.0, f"{took:.1f}s")

    fired: list[bool] = []
    engine._drain()
    feed_live(engine, silence(2.5) + pcm("open notepad") + silence(2.0))
    heard = engine.listen_command(open_window_sec=6.0,
                                  on_speech=lambda: fired.append(True))
    check("a late follow-up is still caught", "notepad" in heard.lower(), repr(heard))
    check("on_speech fires so the HUD can react", fired == [True], fired)


def test_barge_detection(engine: VoiceEngine) -> None:
    print("\nbarge-in detection")
    engine._drain()
    barge = engine._make_barge("Opening Chrome for you, sir.")
    feed(engine, pcm("stop") + silence(0.5))
    check("hears 'stop' over playback", engine._barge_heard(barge) is True)

    engine._drain()
    barge = engine._make_barge("Opening Chrome for you, sir.")
    feed(engine, pcm("the weather looks fine today") + silence(0.5))
    check("ignores ordinary speech", engine._barge_heard(barge) is False)

    engine._drain()
    barge = engine._make_barge("Shall I stop the download, sir?")
    feed(engine, pcm("stop") + silence(0.5))
    check("ignores a word he is saying himself (speaker echo)",
          engine._barge_heard(barge) is False)


def test_wake_interruptions(engine: VoiceEngine) -> None:
    """The two ways out of the wait that aren't a wake word."""
    print("\nwaiting for a reason to wake")
    import threading

    engine._drain()
    hot, announce = threading.Event(), threading.Event()

    hot.set()
    check("the hotkey wakes him", engine.wait_for_wake(wake_event=hot) == "hotkey")
    check("and is consumed, so it fires once", not hot.is_set())

    announce.set()
    why = engine.wait_for_wake(wake_event=hot, announce_event=announce)
    check("something to say wakes him too", why == "announce", why)
    check("the queue is left for the worker to empty", announce.is_set())

    check("a timer goes off even while listening is paused",
          engine.wait_for_wake(is_paused=lambda: True, announce_event=announce)
          == "announce")

    hot.set()
    check("the user asking comes first",
          engine.wait_for_wake(wake_event=hot, announce_event=announce) == "hotkey")
    announce.clear()


def test_level_meter(engine: VoiceEngine) -> None:
    """The HUD's voiceprint has to answer the room, not a canned animation."""
    print("\nlevel meter")
    engine._env.clear()

    quiet = engine.next_level()
    check("a silent mic reads zero", quiet == 0.0, quiet)

    engine._meter(silence(0.5))
    check("silence fills the envelope with silence",
          max(engine._env) < 0.02, max(engine._env))

    engine._env.clear()
    speech = pcm("Stark, are you there?")
    mid = len(speech) // 2 // 2 * 2  # past the lead-in, into the words
    engine._meter(speech[mid:mid + BLOCK_BYTES])
    check("speech fills it with something", max(engine._env) > 0.05,
          max(engine._env))
    check("one block becomes a whole envelope", len(engine._env) >= 20,
          len(engine._env))
    check("and nothing clips past full", max(engine._env) <= 1.0,
          max(engine._env))

    # The HUD asks far more often than blocks arrive, so a starved envelope
    # has to fall away rather than freeze the ring mid-word.
    engine._env.clear()
    engine._env.append(1.0)
    first = engine.next_level()
    second = engine.next_level()
    third = engine.next_level()
    check("the level plays back the sample it was given", first == 1.0, first)
    check("then decays when nothing new arrives",
          0.0 < third < second < first, (second, third))

    # A block too short to slice is ignored rather than raising on the audio
    # driver's own thread.
    engine._env.clear()
    engine._meter(b"\x00\x01")
    check("a runt block is dropped, not fatal", not engine._env)


def test_speaks_a_stream() -> None:
    print("\nspeaking a live stream (played silently)")
    engine = VoiceEngine.__new__(VoiceEngine)  # no mic needed to synthesize
    engine._tts = None
    engine._q = queue.Queue()
    engine._barge_grammar = ""  # no recogniser loaded here, so no barge-in
    pygame = engine._ensure_mixer()
    pygame.mixer.music.set_volume(0.0)  # verify the plumbing without the noise

    played: list[str] = []
    real_play = engine._play_mp3

    def spy(path, barge=None):
        played.append(os.path.basename(path))
        return real_play(path, barge=barge)

    engine._play_mp3 = spy

    def slow_reply():
        """A brain that is still writing while Stark is already talking."""
        for line in ("Right away, sir.", "Chrome is open.", "Anything else?"):
            time.sleep(0.4)
            yield line

    t0 = time.monotonic()
    cut = engine._speak_streamed(slow_reply(), "edge", False)
    took = time.monotonic() - t0
    check("every chunk of a stream is spoken", len(played) == 3, played)
    check("in the order they were written",
          played == ["stark_tts_0.mp3", "stark_tts_1.mp3", "stark_tts_2.mp3"], played)
    check("a stream that is never interrupted reports so", cut is False)
    print(f"    three-sentence stream spoken in {took:.1f}s")

    # A finished string goes out as one clip: splitting it would only add the
    # padding silence the voice service puts around every clip.
    played.clear()
    engine.speak("Right away, sir. Chrome is open. Anything else?")
    engine._play_mp3 = real_play
    pygame.mixer.music.set_volume(1.0)
    check("a finished reply is spoken as a single clip", len(played) == 1, played)


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("ffmpeg is needed to decode the test speech; skipping audio tests.")
        return 1

    test_hotkey_parsing()

    print("\nstarting the recogniser (this opens the mic, then closes it)...")
    engine = VoiceEngine()
    engine._stream.stop()  # feed it synthetic audio, not the room
    engine._drain()

    test_wake_prefix(engine)
    test_barge_suppression(engine)
    test_command_capture(engine)
    test_whisper(engine)
    test_followup_window(engine)
    test_barge_detection(engine)
    test_wake_interruptions(engine)
    test_level_meter(engine)
    engine.close()

    test_speaks_a_stream()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
