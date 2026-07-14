# voice2text

Local push-to-talk dictation for macOS. Hold the **Fn** key, speak, release — the transcript is
pasted into whatever app has focus. A small **listening pill** appears at the bottom-center of your
screen while you hold Fn (a pulsing dot → "Transcribing…" on release). Everything runs on-device
with [whisper.cpp](https://github.com/ggerganov/whisper.cpp) on the GPU (Metal); nothing leaves
your Mac. Target latency from key-release to pasted text: **under 700ms**.

## Requirements

- Apple Silicon Mac (M1 or later) running macOS
- [uv](https://docs.astral.sh/uv/)
- Python 3.11+ (uv will fetch one if needed)

## Install

```bash
git clone <repo-url> voice2text
cd voice2text
uv sync
```

The first run downloads the whisper model (`small.en`, ~466MB) automatically. Subsequent starts
reuse the cached model.

## macOS permissions (read this — it's the part that bites)

The app taps keyboard events, records the mic, and synthesizes a Cmd+V keystroke. macOS gates all
three behind per-app permissions, granted to **whatever app launches the script** (Terminal,
iTerm2, your IDE — not Python, not voice2text).

1. **Input Monitoring** — System Settings → Privacy & Security → Input Monitoring → add and
   enable your terminal app. Needed to see the Fn key.
2. **Accessibility** — System Settings → Privacy & Security → Accessibility → add and enable the
   same app. Needed to post the synthetic Cmd+V.
3. **Microphone** — macOS prompts on the first recording; click Allow. Also under
   System Settings → Privacy & Security → Microphone.

> **macOS never re-prompts after a denial.** If you clicked "Don't Allow" once, or the app just
> silently doesn't work, open System Settings, toggle the permission on manually (or off and on
> again), then **quit and restart your terminal app**. If Input Monitoring is missing, the app
> exits with an error naming the exact settings pane to fix.

### Required keyboard setting

System Settings → Keyboard → "Press 🌐 key to" → **Do Nothing**.

Otherwise macOS's built-in dictation triggers on the same key and fights this app.

## Usage

```bash
uv run voice2text
```

Flags:

- `--verbose` — DEBUG logging (per-stage timing, tap events).
- `--model <name>` — whisper model to use (default `small.en`; also settable via the
  `VOICE2TEXT_MODEL` environment variable).
- `--show-result-window <mode>` — when to show the copy-the-text window: `on-failure` (default),
  `always`, or `never` (also settable via `VOICE2TEXT_RESULT_WINDOW`). See below.
- `--keep-fillers` — keep spoken filler words. By default voice2text strips a conservative set
  (`um`, `uh`, `uhh`, `uhm`, `erm`, `er`) from the transcript; real words are never dropped (e.g.
  "summer" is untouched). Also settable via `VOICE2TEXT_REMOVE_FILLERS=0`.
- `--no-learn` — disable learning custom vocabulary from your in-place corrections (see below).
  Also settable via `VOICE2TEXT_LEARN=0`.

On startup a banner shows the model in use and the first-run permissions checklist, and a log
line reports the model load + warmup time. The app runs with no Dock icon or menu bar. Once it
says it's listening:

1. Click into any text field.
2. **Hold Fn**, speak, **release Fn** — the pill shows "Listening…" then "Transcribing…".
3. The transcript pastes at the cursor.

Note: pasting works by briefly replacing your clipboard. The previous clipboard contents (plain
text only) are restored automatically about 300ms after the paste.

### Custom vocabulary & learning

voice2text keeps a small vocabulary store (JSON at
`~/Library/Application Support/voice2text/vocabulary.json`, override with `VOICE2TEXT_VOCAB`) that
improves accuracy on names and jargon two ways:

- **Biasing** — your terms are fed to whisper as an initial prompt, nudging it toward the right
  spelling (probabilistic — it helps, it doesn't guarantee).
- **Substitutions** — learned `wrong → right` fixes are applied to every transcript afterwards.
  Deterministic: once learned, always applied, in every app.

**Learning from your corrections (automatic, best-effort).** When you dictate and then *fix a word
in place* — e.g. it types "cube control" and you change it to "kubectl" — voice2text notices on your
next dictation and learns `cube control → kubectl`. It reads the corrected text through macOS's
Accessibility API, which works in native text fields (TextEdit, Notes, Mail, many others) but **not
in every app** — notably VS Code / Electron apps and some browser fields don't expose their text, so
corrections there can't be auto-detected. Every learned correction is printed to the log, and you
can undo a wrong one with `forget` (below). Turn the whole thing off with `--no-learn`.

**Managing the vocabulary by hand:**

```bash
uv run python -m voice2text.vocabulary list
uv run python -m voice2text.vocabulary add kubectl
uv run python -m voice2text.vocabulary learn cube control :: kubectl   # wrong :: right
uv run python -m voice2text.vocabulary forget cube control             # undo a learned fix
```

(Capitalization-only fixes like "github" → "GitHub" aren't auto-learned — add the term with `add`
so biasing can pick it up instead.)

### The copy-the-text window

If a paste can't be delivered, the transcript is **never lost** — it's left on your clipboard and a
small window pops up at the bottom-center with the text and a **Copy** button. By default this
appears only on failure (`--show-result-window on-failure`); use `always` to see it after every
dictation, or `never` to suppress it. The most common reason a paste fails silently is **Secure
Keyboard Entry** (see Troubleshooting).

Quit with Ctrl+C in the terminal.

## Standalone module tests (manual verification)

Most of this app (key taps, mic, pasting) can't run in CI, so each module has a standalone mode:

```bash
uv run python -m voice2text.hotkey       # prints "Fn down" / "Fn up" as you press the key
uv run python -m voice2text.recorder     # records 3s from the mic and saves a wav to play back
uv run python -m voice2text.transcriber tests/fixtures/hello.wav   # prints the transcript
uv run python -m voice2text.paster       # pastes a test string via clipboard + Cmd+V
uv run python -m voice2text.overlay      # flashes the listening pill + result window (bottom-center)
uv run python -m voice2text.vocabulary list   # manage custom vocabulary / learned corrections
```

## Running tests

```bash
uv run pytest        # transcription test auto-skips until the model is downloaded
uv run ruff check    # lint
uv run ruff format   # format
```

## Start at login (optional, via launchd)

Running from a terminal is the simpler, recommended mode. If you want it at login anyway, create
`~/Library/LaunchAgents/com.user.voice2text.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.voice2text</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/uv</string>
        <string>run</string>
        <string>--project</string>
        <string>/path/to/voice2text</string>
        <string>voice2text</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/voice2text.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/voice2text.log</string>
</dict>
</plist>
```

Adjust the `uv` path (`which uv`) and the project path, then:

```bash
launchctl load ~/Library/LaunchAgents/com.user.voice2text.plist    # start now + at login
launchctl unload ~/Library/LaunchAgents/com.user.voice2text.plist  # stop + disable
```

**Caveat:** when launched this way, the binary in `ProgramArguments` — `uv` itself — is what needs
the Input Monitoring, Accessibility, and Microphone grants, not your terminal app. Add `uv` to
each pane in System Settings (drag it in from Finder with Cmd+Shift+G if it doesn't appear).
Check `/tmp/voice2text.log` if it doesn't come up.

## Troubleshooting

**Nothing happens when I press Fn.**
In order of likelihood: Input Monitoring not granted to your terminal app (toggle it, restart the
terminal); the 🌐 key setting isn't "Do Nothing" (see above); or the event tap got disabled by
macOS — the app logs a warning and re-enables it, so check the logs with `--verbose`.

**The first word of my dictation is clipped.**
This shouldn't happen — the mic stream stays open permanently and recording just flips a flag, so
there's no stream-startup lag. If you see it, please file an issue with your hardware details.

**Nothing pastes — the pill shows "Transcribing…" and then nothing appears.**
The #1 cause is **Secure Keyboard Entry**, which blocks *all* synthetic keystrokes system-wide.
Check your terminal's menu (Terminal → *Secure Keyboard Entry*, or the equivalent in iTerm) and
turn it off; a focused password field or some security apps can also switch it on temporarily.
voice2text detects this case, leaves the transcript on your clipboard, and pops the copy-the-text
window so you can grab it — but to have it paste automatically, Secure Keyboard Entry must be off.

**Paste doesn't land in a specific app.**
Some password boxes and hardened enterprise apps block synthetic keystrokes even without global
secure input. The text is still on the clipboard for the ~300ms restore window; paste manually, or
run with `--show-result-window always` to always get the copy window.

**I tapped Fn by accident and got weird text like "Thank you."**
Whisper hallucinates on near-silent audio. Utterances shorter than 0.3s are dropped by design, so
accidental taps should produce nothing — if hallucinated text still gets through, it means you
held the key just long enough to record silence.

**The model re-downloads every run.**
pywhispercpp caches models under `~/Library/Application Support/pywhispercpp/models/`. If that
directory isn't writable or gets cleaned by a disk utility, the download repeats. Check the
directory exists and contains `ggml-small.en.bin`.
