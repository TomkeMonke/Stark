# Stark - your "Hey Stark" voice assistant

A Jarvis-style voice assistant for Windows. Say **"Hey Stark"** and a glowing
arc-reactor HUD fades into the centre of your screen. Speak a command - open an
app, search the web, ask a question, control the system - and Stark acts and
replies out loud.

It holds a conversation rather than taking one order at a time:

- **Follow-ups.** After he answers, the microphone stays open for a few seconds
  (a draining ring on the HUD shows how long), so the next thing you say needs
  no wake word. "Open Chrome" ... "now search for the weather".
- **Interrupt him.** Say "stop" (or "stark", "cancel", "quiet", "enough",
  "wait") while he's talking and he stops mid-sentence and listens. Words he is
  saying himself are ignored, so the speakers can't interrupt him for you.
- **He starts talking sooner.** The first sentence is spoken while Gemini is
  still writing the rest, so an acknowledgement is out loud before the action it
  refers to has even run.
- **Push-to-talk.** `Ctrl+Alt+S` wakes him without saying his name - handy in a
  loud room, or on a call. It works even while listening is paused.

And he is no longer limited to what he already knew:

- **He can see your screen.** "What's on my screen", "read me this error", "what
  am I looking at" - he takes a screenshot and answers from it.
- **He can look things up.** Weather, news, prices, scores, opening hours: he
  answers out loud from a live web search rather than opening a tab, and prefers
  it over his own knowledge whenever being out of date would matter.
- **He remembers.** "Remember I park in level B2" sticks across restarts, and
  "forget about the car" drops it again.

And he keeps working while you do:

- **Timers and reminders.** "Set a timer for ten minutes for the pasta",
  "remind me in an hour to call the dentist". They go off out loud whatever
  you are doing, they survive a restart, and one that came due while Stark was
  closed is announced the moment he is back, with how late it is.
- **Media keys.** "Pause", "skip this one", "previous song" - it drives
  whatever is playing, Spotify or a video in a tab, without needing to know
  which.
- **The clipboard.** "What's on my clipboard", "copy that address down",
  "translate what I copied", "paste it".

## How it works

| Piece | Tech |
|---|---|
| Wake word + when you start/stop talking | [Vosk](https://alphacephei.com/vosk/) (offline, free) |
| What you actually said | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) `base.en` (offline, free) |
| Brain (understands & decides) | Google Gemini (free tier, function calling) |
| Eyes and live answers | Gemini vision + Google Search grounding |
| Voice | edge-tts British neural (free) → pyttsx3/SAPI5 offline fallback; ElevenLabs optional |
| HUD | PySide6 (Qt) - transparent, always-on-top, click-through |

The Qt HUD runs on the main thread; a background thread listens → thinks →
speaks and updates the HUD via Qt signals.

## Setup

Already done for you: a virtual environment in `.venv` with all dependencies,
and the Vosk speech model in `models/`. The Whisper model (~145 MB) downloads
itself the first time Stark runs.

**You only need a free Gemini API key.** Get one at
<https://aistudio.google.com/apikey> (no credit card), then either:

```powershell
setx GEMINI_API_KEY "..."
```
(reopen the terminal afterwards), **or** put it in `config.json`:

```json
{ "gemini_api_key": "..." }
```

## Run

Double-click **`run_stark.bat`**, or:

```powershell
.\.venv\Scripts\python.exe stark.py
```

You'll hear "Stark online." Then say **"Hey Stark"**, wait for the ring, and
speak. Example commands:

- "Open Chrome"
- "Search the web for the weather in Warsaw"
- "Open my Documents folder"
- "Turn the volume up"
- "Take a screenshot"
- "Lock the computer"
- "What's the capital of Australia?"
- "What's on my screen?" · "Read me this error"
- "What's the weather in Warsaw?" · "Who won the match last night?"
- "Remember that I park in level B2" · "What do you know about me?"
- "Set a timer for ten minutes" · "How long is left?" · "Cancel the timer"
- "Remind me in an hour to call the dentist"
- "Pause" · "Next song" · "Previous track"
- "What's on my clipboard?" · "Translate what I copied"

## Screen & sound: the Quick Control panel

Stark drives [Quick Control](../QuickControl) - the pop-up settings panel - for
anything to do with the screen, sound or window layout, so the panel's sliders
always show what you asked for out loud:

- "Set the brightness to forty" · "Brightness down a bit"
- "Turn on night mode" · "Warmth seventy" · "Warm mode off"
- "Set the volume to twenty-five" · "Mute" · "Put Spotify at ten percent"
- "Run the movie preset" · "Keep the PC awake" · "Let it sleep"
- "Arrange my windows for coding" (a layout you saved in the panel)
- "What's my brightness?" · "Open Quick Control"

Stark talks to the running panel over its local named pipe, and starts it if it
isn't up (set `quickcontrol_autostart` to `false` to stop that). If you keep
Quick Control somewhere other than next to this folder, set `quickcontrol_dir`.
Volume verbs go through the panel when it's running and fall back to the media
keys when it isn't.

## Configure

Edit `config.json` (created from defaults - see `config.py`):

- `wake_words` - phrases that activate Stark (default includes "stark", "hey stark").
- `gemini_model` - Gemini model (default `gemini-2.5-flash-lite`, which has a
  much larger free-tier daily quota than `gemini-2.5-flash`).
- **Voice:** `tts_engine` is `"edge"` (default, free British neural voice),
  `"elevenlabs"` (paid, most cinematic), or `"sapi"` (offline).
  - edge: `edge_voice` (default `en-GB-RyanNeural`; try `en-GB-ThomasNeural`,
    `en-GB-SoniaNeural`), `edge_rate`, `edge_pitch`.
  - ElevenLabs: set `tts_engine` to `"elevenlabs"`, add `ELEVENLABS_API_KEY`
    (or `elevenlabs_api_key` in config.json) and optionally `elevenlabs_voice_id`.
  - Offline fallback always available: `tts_rate`, `tts_voice_hint`.
  - If edge/ElevenLabs fail (e.g. no internet), Stark falls back to the offline voice.
- `command_timeout_sec` / `command_silence_sec` - how long it listens.
- **Transcription:** `stt_engine` is `"whisper"` (default, accurate, costs about
  0.6s after you stop speaking and a ~145 MB model on first use) or `"vosk"`
  (instant, rougher - it hears "arrange my windows" as "a range my windows").
  `whisper_model` can be `tiny.en` for speed or `small.en` for accuracy. If
  Whisper can't load for any reason, Stark falls back to Vosk rather than going
  deaf.
- **Second-request tools:** `vision_model` and `search_model` override which
  model looks at the screen and which answers from the web. Blank means
  `gemini_model`. Looking at the screen, answering from the web and doing
  something with the clipboard each cost one extra request for that command.
- **Timers:** `timer_chime` - a ping before a timer speaks, so an announcement
  out of nowhere doesn't startle you.
- `clipboard_speak_chars` - how much of the clipboard to read back before
  cutting it short (default 400; anything longer is a wall of text nobody
  wants read to them).
- **Conversation:** `followup_enabled` and `followup_window_sec` (default 7)
  control the no-wake-word window; set the window to `0` to turn follow-ups off.
- **Interrupting:** `barge_in` on/off, and `barge_words` - the words that cut
  him off. Barge-in needs a neural voice; the offline SAPI fallback can't be
  stopped part-way. If your speakers bleed badly into the mic, set it to `false`.
- `stream_speech` - start speaking sentence one while the model writes the rest.
  Off means one finished answer, one clip.
- **Push-to-talk:** `hotkey_enabled` and `hotkey` (default `ctrl+alt+s`). If
  another app already owns the combination, Stark says so in the log and carries
  on answering to his name.
- `hud_size` - diameter of the reactor in pixels.
- `quickcontrol_dir` - where Quick Control lives (blank = look next to Stark).
- `quickcontrol_autostart` - start Quick Control when a spoken command needs it.

## Start automatically at login (optional)

Press `Win+R`, type `shell:startup`, and drop a shortcut to `run_stark.bat`
there. For no console window, point the shortcut at:
`.venv\Scripts\pythonw.exe stark.py`.

## Tests

No microphone, no API key and no sound needed - test speech is synthesized with
the same voice Stark speaks with and fed straight into the recogniser, and the
brain runs against a stubbed client.

```powershell
.\.venv\Scripts\python.exe _test_voice.py    # wake, listening, barge-in, speech
.\.venv\Scripts\python.exe _test_brain.py    # streaming, tool calls, errors
.\.venv\Scripts\python.exe _test_flow.py     # follow-ups and interruptions
.\.venv\Scripts\python.exe _test_tools.py    # memory, screen, web, clipboard
.\.venv\Scripts\python.exe _test_timers.py   # timers, reminders, announcements
.\.venv\Scripts\python.exe _test_hotkey.py   # push-to-talk really fires
.\.venv\Scripts\python.exe _test_hud.py      # renders _hud_*.png to look at
```

`_test_voice.py` needs `ffmpeg` on PATH to decode the test speech.

## Notes & limits

- Wake-word matching is keyword-based on Vosk's transcription - "Stark" alone
  also triggers it. Add/adjust phrases in `wake_words`.
- Barge-in listens for whole words, so it can only be as good as Vosk is in a
  room with speakers playing. It is deliberately restricted to a short word list
  for that reason.
- Looking at the screen, answering from the web, and transforming what you
  copied each cost a second API request for that command; everything else is
  exactly one. Reading the clipboard back verbatim costs nothing.
- A timer never cuts into a conversation - it waits for the gap, so it can be
  a few seconds late if you are mid-sentence. It does go off while listening
  is paused, on the grounds that you asked for it yourself. Pending timers
  live in `timers.json`, which is git-ignored.
- What Stark remembers lives in `memory.json`, which is git-ignored. Delete the
  file to wipe it, or say "forget everything".
- Shutdown/restart run on a 5-second delay; say "Hey Stark, cancel shutdown".
- Everything except the brain runs offline. The brain needs internet + the key.
