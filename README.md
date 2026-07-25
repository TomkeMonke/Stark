# Stark — your "Hey Stark" voice assistant

A Jarvis-style voice assistant for Windows. Say **"Hey Stark"** and a glowing
arc-reactor HUD fades into the centre of your screen. Speak a command — open an
app, search the web, ask a question, control the system — and Stark acts and
replies out loud.

## How it works

| Piece | Tech |
|---|---|
| Wake word + speech-to-text | [Vosk](https://alphacephei.com/vosk/) (offline, free) |
| Brain (understands & decides) | Google Gemini (free tier, function calling) |
| Voice | edge-tts British neural (free) → pyttsx3/SAPI5 offline fallback; ElevenLabs optional |
| HUD | PySide6 (Qt) — transparent, always-on-top, click-through |

The Qt HUD runs on the main thread; a background thread listens → thinks →
speaks and updates the HUD via Qt signals.

## Setup

Already done for you: a virtual environment in `.venv` with all dependencies,
and the Vosk speech model in `models/`.

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

## Screen & sound: the Quick Control panel

Stark drives [Quick Control](../QuickControl) — the pop-up settings panel — for
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

Edit `config.json` (created from defaults — see `config.py`):

- `wake_words` — phrases that activate Stark (default includes "stark", "hey stark").
- `gemini_model` — Gemini model (default `gemini-2.5-flash`, fast & free-tier).
- **Voice:** `tts_engine` is `"edge"` (default, free British neural voice),
  `"elevenlabs"` (paid, most cinematic), or `"sapi"` (offline).
  - edge: `edge_voice` (default `en-GB-RyanNeural`; try `en-GB-ThomasNeural`,
    `en-GB-SoniaNeural`), `edge_rate`, `edge_pitch`.
  - ElevenLabs: set `tts_engine` to `"elevenlabs"`, add `ELEVENLABS_API_KEY`
    (or `elevenlabs_api_key` in config.json) and optionally `elevenlabs_voice_id`.
  - Offline fallback always available: `tts_rate`, `tts_voice_hint`.
  - If edge/ElevenLabs fail (e.g. no internet), Stark falls back to the offline voice.
- `command_timeout_sec` / `command_silence_sec` — how long it listens.
- `hud_size` — diameter of the reactor in pixels.
- `quickcontrol_dir` — where Quick Control lives (blank = look next to Stark).
- `quickcontrol_autostart` — start Quick Control when a spoken command needs it.

## Start automatically at login (optional)

Press `Win+R`, type `shell:startup`, and drop a shortcut to `run_stark.bat`
there. For no console window, point the shortcut at:
`.venv\Scripts\pythonw.exe stark.py`.

## Notes & limits

- Wake-word matching is keyword-based on Vosk's transcription — "Stark" alone
  also triggers it. Add/adjust phrases in `wake_words`.
- Shutdown/restart run on a 5-second delay; say "Hey Stark, cancel shutdown".
- Everything except the brain runs offline. The brain needs internet + the key.
