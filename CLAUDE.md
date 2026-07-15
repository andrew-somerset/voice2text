# CLAUDE.md — voice2text

Local push-to-talk dictation for macOS (a self-built Wispr Flow). Hold the **Fn key**, speak, release — the transcript is pasted into whatever app has focus. Runs entirely on-device using whisper.cpp on Apple Silicon. Target latency from key-release to pasted text: **under 700ms**.

## Project status

Greenfield. Nothing is built yet. Follow the milestones at the bottom in order — each milestone is independently testable before wiring into the next.

## Tech stack

- **Python 3.11+** — all application code. Do NOT suggest rewriting in C++/Rust/Swift; the heavy lifting is already native (whisper.cpp is C++). Python is glue only.
- **pywhispercpp** — Python bindings for whisper.cpp with Metal (GPU) support. This is the transcription engine.
- **pyobjc** (Quartz / AppKit frameworks) — Fn key event tap and synthetic Cmd+V keystroke.
- **sounddevice** — microphone capture.
- **uv** for dependency management (`uv sync`, `uv run`). Keep deps in `pyproject.toml`.
- Whisper model: **small.en** by default (best speed/accuracy tradeoff for dictation). Model name must be a config constant, not hardcoded inline.

## Repo layout

```
voice2text/
├── CLAUDE.md
├── pyproject.toml
├── README.md            # setup + permissions instructions for humans
├── src/voice2text/
│   ├── __init__.py
│   ├── main.py          # entry point, wires components, owns threads
│   ├── hotkey.py        # Quartz event tap for Fn key down/up
│   ├── recorder.py      # sounddevice mic capture into a buffer
│   ├── transcriber.py   # pywhispercpp wrapper, model loaded once
│   ├── paster.py        # clipboard write + synthetic Cmd+V
│   └── config.py        # model name, sample rate, hotkey, constants
└── tests/
    ├── test_transcriber.py   # transcribe a fixture wav, assert text
    ├── test_recorder.py
    └── fixtures/hello.wav    # short known-content recording
```

## Architecture

One long-running process, three concerns, strict data flow:

```
Fn down ──▶ recorder.start()
Fn up   ──▶ recorder.stop() ──▶ audio buffer (16kHz mono float32 numpy)
        ──▶ transcriber.transcribe(buffer) ──▶ text
        ──▶ paster.paste(text)  # clipboard + synthetic Cmd+V
```

Threading model (important — get this right):

- **Main thread**: runs the Quartz event tap run loop (`CFRunLoopRun`). The tap callback must return in microseconds — it only flips a flag / signals a queue. **Never do audio processing or inference in the tap callback**; macOS silently disables event taps whose callbacks are slow (`kCGEventTapDisabledByTimeout`). Handle that event type by re-enabling the tap.
- **Audio**: sounddevice's own callback thread appends frames to a thread-safe buffer.
- **Worker thread**: waits on key-up signal, runs transcription, then pastes. One `queue.Queue` between hotkey and worker is enough. No asyncio — it buys nothing here.

## GM Ask Glean interaction (`gm_dev`)

Keep ordinary dictation push-to-talk, but make Ask Glean a latched recording mode:

- **Local dictation**: hold Fn, speak, release Fn. Transcribe locally and paste into the focused app.
- **Start Ask Glean**: tap and release Fn twice within `FN_DOUBLE_TAP_WINDOW_SECONDS`. Recording starts only after the second release, after the app clears all provisional tap audio. Play a distinct start sound and show a visible "Ask Glean — recording" indicator.
- **Stop Ask Glean**: press Fn a third time. Stop recording immediately on that key-down, ignore the matching key-up, transcribe locally, and submit only the final text to Glean. Play a stop sound and change the indicator to "Thinking".
- A single short tap expires silently and never transcribes or pastes anything.
- Enforce `GLEAN_MAX_RECORDING_SECONDS`; if it expires, stop and ask the user whether to submit rather than sending unexpectedly.

Use configurable starting values of `FN_TAP_MAX_SECONDS = 0.25`, `FN_DOUBLE_TAP_WINDOW_SECONDS = 0.35`, and `GLEAN_MAX_RECORDING_SECONDS = 120.0`. Measure intervals with `time.monotonic_ns()`, not wall-clock time, and tune the tap values through real-Mac testing.

The hotkey state machine must distinguish the gestures without delaying normal dictation:

| State | Input | Action | Next state |
|---|---|---|---|
| `IDLE` | Fn down | Start a provisional local buffer | `FIRST_PRESS` |
| `FIRST_PRESS` | Fn up after a hold | Stop and queue local transcription | `IDLE` |
| `FIRST_PRESS` | Fn up after a short tap | Discard provisional audio and start the double-tap deadline | `WAITING_SECOND_TAP` |
| `WAITING_SECOND_TAP` | Deadline expires | Do nothing | `IDLE` |
| `WAITING_SECOND_TAP` | Fn down before deadline | Start a new provisional buffer | `SECOND_PRESS` |
| `SECOND_PRESS` | Fn up after a short tap | Discard/clear provisional audio, start a fresh Glean buffer, and signal the start UI | `GLEAN_RECORDING` |
| `SECOND_PRESS` | Fn up after a hold | Treat it as ordinary local dictation, which recovers naturally from an accidental first tap | `IDLE` |
| `GLEAN_RECORDING` | Fn down | Stop and queue local transcription plus Glean submission | `GLEAN_STOP_PRESS` |
| `GLEAN_STOP_PRESS` | Fn up | Consume the release without starting another recording | `IDLE` |

The event-tap callback may update this state and enqueue small immutable events such as `LOCAL_START`, `LOCAL_STOP`, `GLEAN_START`, and `GLEAN_STOP`; it must not clear audio, call Whisper, make a network request, play sounds, or update UI directly. Queue consumers perform those actions in event order.

Ask Glean data flow:

```
Fn double tap ──▶ local recorder ──▶ third Fn press ──▶ local transcription
              ──▶ final text over TLS to Glean ──▶ streamed answer + citations
```

Raw microphone audio never leaves the Mac. The final query text does leave the device and is governed by GM/Glean retention and audit policy. Never log audio, transcripts, answers, citations, or OAuth tokens.

## Critical implementation details

These are hard-won macOS specifics. Do not deviate without testing on real hardware.

### Fn key capture (hotkey.py)
- Fn is a **modifier**, not a regular key. It arrives as `kCGEventFlagsChanged`, not keyDown/keyUp. Listen for `flagsChanged` and check `kCGEventFlagMaskSecondaryFn` (0x800000) in the event flags: flag present = pressed, absent = released.
- Use `CGEventTapCreate` at the session level (`kCGSessionEventTap`) as a **listen-only** tap (`kCGEventTapOptionListenOnly`) — we never swallow the event.
- `pynput` does NOT reliably capture Fn on macOS. Use Quartz directly via pyobjc.
- Guard against other modifiers: Fn+arrow (page up/down etc.) also fires flagsChanged. Only trigger when Fn is the change and debounce transitions (track previous flag state).
- Implement the `gm_dev` tap/hold behavior as an explicit, unit-tested state machine. Do not use `sleep()` in the event callback; compare monotonic timestamps and handle expired deadlines from the run loop or a lightweight timer event.

### Transcription (transcriber.py)
- Load the model **once at startup** and keep it resident. Per-utterance model loading costs 1–2s and destroys the latency budget.
- Run one **dummy inference on ~1s of silence at startup** — first Metal inference has ~1s of kernel-compilation warmup; eat that cost before the user's first real utterance.
- pywhispercpp params: `language="en"`, single segment output joined; disable printing/progress. Pass `n_threads` = performance core count.
- Input contract: 16kHz mono float32 numpy array in [-1, 1]. Whisper requires exactly this — resample nothing, record at 16kHz from the start.
- Drop utterances shorter than ~0.3s (accidental taps) — whisper hallucinates on near-empty audio ("Thank you." on silence is the classic failure). Also strip leading/trailing whitespace and discard results that are only punctuation.

### Audio (recorder.py)
- `sounddevice.InputStream`, samplerate=16000, channels=1, dtype="float32". Keep the stream **open permanently** and discard frames when not recording — opening a stream on key-down adds 100–300ms of latency and can clip the first word. Recording toggles a flag, not the stream.
- Buffer as a list of numpy chunks appended in the callback; concatenate on stop.

### Pasting (paster.py)
- Write text to `NSPasteboard.generalPasteboard()` (clear, then `setString_forType_` with `NSPasteboardTypeString`).
- Synthesize Cmd+V with `CGEventCreateKeyboardEvent` (keycode 9 = V), setting `kCGEventFlagMaskCommand`, post keyDown then keyUp to `kCGHIDEventTap`. Small sleep (~50ms) between clipboard write and paste — some apps race.
- Save the previous clipboard string before overwriting and restore it ~300ms after pasting. Only handle the plain-string case; don't try to preserve rich clipboard types in v1.
- Do NOT use per-character `CGEventKeyboardSetUnicodeString` typing as the primary path — it's slower and drops characters in some apps. It may be added later as a fallback config option.

### Permissions (document in README, check at startup)
- The host app (Terminal / iTerm — whatever launches the script) needs **Input Monitoring** and **Accessibility** in System Settings → Privacy & Security, plus **Microphone** on first record.
- macOS never re-prompts after a denial — users must fix it manually in System Settings. At startup, verify the event tap was created successfully (`CGEventTapCreate` returns None on missing permission) and print an actionable error naming the exact settings pane.
- User must set System Settings → Keyboard → "Press 🌐 key to" → **Do Nothing**, or macOS's built-in dictation fights ours. Mention this in README and in the startup banner.

## Commands

```bash
uv sync                          # install deps
uv run voice2text                # run the app (entry point in pyproject)
uv run pytest                    # run tests
uv run python -m voice2text.hotkey       # standalone: prints Fn down/up
uv run python -m voice2text.recorder     # standalone: record 3s, save wav
uv run python -m voice2text.transcriber tests/fixtures/hello.wav  # standalone
```

Every module in `src/voice2text/` should have an `if __name__ == "__main__":` block for standalone manual testing, since most functionality (key taps, mic, paste) can't run in CI.

## Testing

- CI-safe tests: transcriber against fixture wavs (skip if model not downloaded), buffer logic in recorder, config sanity. Mock Quartz/AppKit — never import-fail on Linux CI.
- Unit-test the hotkey state machine without Quartz: hold/release emits local dictation; one tap times out silently; two taps emit exactly one `GLEAN_START`; the third Fn down emits exactly one `GLEAN_STOP`; its release is consumed; and an accidental first tap followed by a hold still emits local dictation.
- Test boundary timestamps exactly at and immediately around both tap thresholds, duplicate `flagsChanged` events, unrelated modifier changes, timeout cancellation, and the maximum Glean recording duration.
- Everything touching real input devices or event taps is manual-test only via the `__main__` blocks above. Document manual test steps in the PR description.
- **Claude Code cannot fully verify this app end-to-end** (no mic, no Fn key, no GUI in the sandbox). After changes to hotkey.py or paster.py, say explicitly what needs manual verification instead of claiming it works.

## Code style

- Type hints everywhere; `ruff` for lint + format (`uv run ruff check`, `uv run ruff format`).
- Each module exposes a small class (`HotkeyListener`, `Recorder`, `Transcriber`, `Paster`) with no cross-imports between sibling modules — only `main.py` wires them together. Keep it this way; it's what makes standalone testing possible.
- Log with `logging`, not print (except the startup banner). One `--verbose` flag flips to DEBUG.
- No global mutable state outside class instances.

## Milestones (build in this order)

1. **Skeleton** — pyproject, package layout, config.py, empty classes, ruff + pytest wired, README with permission setup.
2. **Hotkey** — Fn down/up detection printing to console. Handle tap-disabled re-enable. Manual test.
3. **Recorder** — always-open stream, flag-gated buffering, save-to-wav standalone mode. Manual test: record, play back, verify no clipped first word.
4. **Transcriber** — warm-loaded model, dummy warmup inference, fixture wav test passing, short-utterance rejection.
5. **Paster** — clipboard + Cmd+V with save/restore. Manual test in TextEdit and in a browser text field.
6. **Integration** — main.py wiring, worker thread, queue, startup permission checks and banner. Full manual end-to-end test.
7. **Polish** — latency measurement logging (key-up → paste, per stage), config for model choice, launchd plist or `--daemon` docs so it starts at login.

Do not start a milestone until the previous one's manual test passes. Keep PRs to one milestone each.

## Known future work (do not build unless asked)

- Streaming partial inference while the key is held (cuts perceived latency).
- VAD-based silence trimming.
- Menu bar UI (rumps, or a Swift rewrite).
- Alternative engines (Parakeet via MLX) behind the same Transcriber interface.
- Custom vocabulary / initial-prompt biasing for names and jargon.
