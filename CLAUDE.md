# CLAUDE.md — voice2text GM Windows client

Windows 11 local voice dictation plus a secure voice route into Glean. Hold a configurable **trigger key**, speak, and release to paste a local transcript into the focused application. Double-tap the trigger to start Ask Glean recording, then tap it a third time to stop and submit. Raw audio and speech recognition remain on-device; only the final Ask Glean query text is sent to GM's Glean tenant.

This file describes the Windows-first `gm_dev` branch. The personal macOS design remains on `main`; do not carry Quartz, AppKit, Metal, macOS permissions, or Apple packaging assumptions into this branch.

## Project status

Greenfield in this checkout. Build and test the Windows-safe milestones locally now. Live Glean authentication remains behind an interface and a mock until GM's Glean administrators register the integration.

## Tech stack

- **Python 3.11+** — all application code. Do NOT suggest rewriting in C++/Rust/Swift; the heavy lifting is already native (whisper.cpp is C++). Python is glue only.
- **pywhispercpp** — Python bindings for whisper.cpp. Use the prebuilt CPU wheel as the portable baseline; treat Vulkan, CUDA, and OpenVINO as separately reviewed optimizations, not requirements.
- **Win32 APIs through `ctypes`** — Raw Input, clipboard, `SendInput`, and DPAPI. Avoid a broad keyboard-hook package.
- **sounddevice** — microphone capture through Windows WASAPI shared mode.
- **numpy** — mono `float32` audio buffers.
- **httpx** — HTTPS streaming for the Glean Client Chat API. Keep OAuth flow logic behind an interface so the live implementation can be enabled after admin registration.
- **uv** for dependency management (`uv sync`, `uv run`). Keep deps in `pyproject.toml`.
- Whisper model: benchmark **tiny.en**, **base.en**, and a reviewed quantized **small.en** model on representative GM laptops before selecting the managed default. The model path and checksum must be configuration, never an automatic production download.

## Repo layout

```
voice2text/
├── CLAUDE.md
├── pyproject.toml
├── README.md            # setup + permissions instructions for humans
├── src/voice2text/
│   ├── __init__.py
│   ├── main.py          # entry point, wires components, owns threads
│   ├── gesture.py       # pure trigger state machine; no Win32 imports
│   ├── hotkey.py        # Windows Raw Input adapter for trigger down/up
│   ├── recorder.py      # WASAPI/sounddevice capture into a buffer
│   ├── transcriber.py   # pywhispercpp wrapper, model loaded once
│   ├── paster.py        # Windows clipboard + SendInput Ctrl+V
│   ├── auth.py          # OAuth Authorization Code + PKCE and DPAPI storage
│   ├── glean_client.py  # mockable Glean Chat API interface
│   ├── overlay.py       # recording/thinking/answer UI with citations
│   └── config.py        # model, trigger, sample rate, timing, endpoints
└── tests/
    ├── test_gesture.py
    ├── test_transcriber.py   # transcribe a fixture wav, assert text
    ├── test_recorder.py
    ├── test_glean_client.py
    └── fixtures/hello.wav    # short known-content recording
```

## Architecture

One long-running Windows desktop process with two explicit routes:

```
Local: trigger hold ──▶ local audio ──▶ local Whisper ──▶ focused app

Glean: trigger double tap ──▶ local audio ──▶ third trigger tap
       ──▶ local Whisper ──▶ query text over TLS ──▶ Glean answer overlay
```

Threading model:

- **Windows message thread**: owns the hidden message-only window and processes `WM_INPUT`. It filters immediately for the configured trigger and enqueues tiny immutable events. It never handles audio, inference, HTTP, sounds, or UI rendering.
- **Audio callback thread**: appends mono `float32` frames to a lock-protected chunk list only while recording.
- **Transcription worker**: consumes ordered start/stop events, finalizes audio, runs local Whisper, and routes the text to paste or Glean.
- **Glean worker**: performs OAuth refresh and streams Chat API responses without blocking input or transcription.
- **UI thread**: owns the recording/thinking/answer overlay. Cross-thread updates use a queue.

## Trigger interaction

Windows laptop Fn keys are commonly handled by keyboard firmware or OEM utilities and often never appear as a normal Windows key event. **Do not make Fn a requirement.** Use a configurable trigger represented by a scan code plus extended-key flag. Start the prototype with **Right Ctrl**, which is a standard distinguishable key and has no standalone action. A managed deployment can change the trigger after testing GM laptop models.

- **Local dictation**: hold the trigger, speak, release. Transcribe locally and paste into the focused application.
- **Start Ask Glean**: tap and release the trigger twice within `DOUBLE_TAP_WINDOW_SECONDS`. Start recording only after the second release, after clearing all provisional tap audio. Show an unmistakable "Ask Glean — recording" indicator and play a distinct start sound.
- **Stop Ask Glean**: press the trigger a third time. Stop immediately on key-down, ignore its matching key-up, transcribe locally, then submit the final text. Change the indicator to "Thinking".
- A single short tap expires silently and never transcribes or pastes.
- At `GLEAN_MAX_RECORDING_SECONDS`, stop and require explicit confirmation rather than submitting unexpectedly.

Use configurable starting values of `TAP_MAX_SECONDS = 0.25`, `DOUBLE_TAP_WINDOW_SECONDS = 0.35`, and `GLEAN_MAX_RECORDING_SECONDS = 120.0`. Measure with `time.monotonic_ns()` and tune on representative GM hardware.

| State | Input | Action | Next state |
|---|---|---|---|
| `IDLE` | Trigger down | Start provisional local recording | `FIRST_PRESS` |
| `FIRST_PRESS` | Trigger up after a hold | Stop and queue local transcription | `IDLE` |
| `FIRST_PRESS` | Trigger up after a short tap | Discard audio and start double-tap deadline | `WAITING_SECOND_TAP` |
| `WAITING_SECOND_TAP` | Deadline expires | Do nothing | `IDLE` |
| `WAITING_SECOND_TAP` | Trigger down before deadline | Start a new provisional buffer | `SECOND_PRESS` |
| `SECOND_PRESS` | Trigger up after a short tap | Discard tap audio and start a fresh Glean recording | `GLEAN_RECORDING` |
| `SECOND_PRESS` | Trigger up after a hold | Treat as local dictation after an accidental first tap | `IDLE` |
| `GLEAN_RECORDING` | Trigger down | Stop and queue transcription plus Glean submission | `GLEAN_STOP_PRESS` |
| `GLEAN_STOP_PRESS` | Trigger up | Consume release without restarting | `IDLE` |

The gesture state machine is pure Python and accepts timestamped `DOWN`, `UP`, and `TIMER` inputs. Platform code translates Windows input into those events. This separation makes every timing boundary testable without a keyboard.

## Critical implementation details

These Windows details are security and latency requirements. Do not substitute broad convenience libraries without review.

### Windows trigger input (hotkey.py)
- Prefer Win32 **Raw Input** through a hidden message-only window registered with `RIDEV_INPUTSINK`. Microsoft recommends Raw Input over a low-level keyboard hook for most monitoring cases.
- Do not use `WH_KEYBOARD_LL`, `pynput`, or the `keyboard` package in the baseline. A global hook sees every keystroke, resembles keylogger behavior to security tools, participates in a shared hook chain, and can be silently removed when its callback is slow.
- Raw Input also receives keyboard events broadly. Inspect only enough `RAWKEYBOARD` data to match the configured scan code and `RI_KEY_E0`/`RI_KEY_E1` flag. Immediately discard every non-trigger event. Never convert other keys to text, store them, count them, or log them.
- Do not use `RIDEV_NOLEGACY`; the app is listen-only and must not suppress input to other applications.
- De-duplicate key auto-repeat and repeated make/break messages. Emit one transition only when the trigger state actually changes.
- If security review rejects background Raw Input, provide a lower-capability fallback based on `RegisterHotKey`; do not quietly replace it with a global hook.
- Treat OEM Fn support as optional future hardware validation. If a particular GM laptop emits Fn as raw HID, add an allowlisted device-specific mapping rather than making it universal behavior.

### Transcription (transcriber.py)
- Load the model **once at startup** and keep it resident. Per-utterance model loading costs 1–2s and destroys the latency budget.
- Run one dummy inference on approximately one second of silence at startup so allocations and native initialization occur before the first utterance.
- Use `language="en"`, join segment text, disable printing/progress, and benchmark `n_threads` rather than assuming every logical core is faster.
- Input contract: 16kHz mono float32 numpy array in [-1, 1]. Whisper requires exactly this — resample nothing, record at 16kHz from the start.
- Drop utterances shorter than ~0.3s (accidental taps) — whisper hallucinates on near-empty audio ("Thank you." on silence is the classic failure). Also strip leading/trailing whitespace and discard results that are only punctuation.
- Production must load a locally managed model by path and verify its SHA-256 checksum before use. Never download a model at runtime on a GM endpoint.
- Record benchmark results by laptop model, CPU, Whisper model, thread count, utterance duration, and release-to-result latency. Do not claim the 700ms target until measured on representative hardware.

### Audio (recorder.py)
- Use `sounddevice.InputStream`, channels=1, dtype="float32", through WASAPI shared mode.
- Open and configure the stream object once, but activate it only during recording so Windows does not show the microphone as continuously in use. Measure key-down-to-first-frame latency and clipping on GM hardware.
- Request 16kHz when supported. If a managed microphone only supports another shared-mode rate, fail with an actionable diagnostic until a reviewed resampler is added; do not silently pass non-16kHz audio to Whisper.
- Buffer as a list of numpy chunks appended in the callback; concatenate on stop.
- Keep audio in memory only. Zero/drop references after transcription and never write temporary WAV files outside explicit manual-test commands.

### Pasting (paster.py)
- Write UTF-16 text through the Win32 clipboard APIs using `CF_UNICODETEXT`, then synthesize Ctrl+V with `SendInput`.
- Save and restore only a previous plain-text clipboard value. Do not claim preservation of rich clipboard formats in v1.
- Retry `OpenClipboard` briefly because another process may own it. Bound all retries and surface a failure instead of hanging.
- Add a short configurable clipboard-to-paste delay and restore delay; test Teams, Outlook, browsers, Office, and text editors.
- Never paste Glean answers automatically. Render answers and citations in the overlay and require an explicit copy action.

### Glean authentication and API (auth.py, glean_client.py)
- Use the official Glean Client Chat API over HTTPS. Do not automate a browser, scrape the Glean UI, embed a shared API token, or add a local MCP package for this narrow workflow.
- Authenticate each employee with OAuth Authorization Code + PKCE in the system browser. Use a loopback redirect on `127.0.0.1`, validate `state`, and never include a client secret in the desktop app.
- Request only the approved `CHAT` scope and `offline_access` only if GM allows refresh tokens. Glean must enforce the signed-in user's existing permissions.
- Protect refresh tokens for the current Windows user with DPAPI (`CryptProtectData`) or Windows Credential Manager. Never use machine-wide DPAPI protection, plaintext files, environment variables, logs, or source control for production tokens.
- Discover tenant OAuth endpoints from server metadata. Tenant URL, client ID, redirect URI, scopes, and optional chat application ID are managed configuration—not source constants.
- Stream answers and citation metadata. Handle 401 refresh, 403 policy denial, 408 timeout, and 429 rate limiting without exposing response bodies in logs.
- Whether Glean saves a chat is an administrator-controlled policy. `saveChat=false` does not imply that audit or retention records do not exist.

### Data boundaries and logging
- Local route: no network request.
- Glean route: raw audio stays local; only final query text is transmitted after the stop gesture.
- Never persist audio, transcripts, Glean prompts, answers, source snippets, clipboard contents, or tokens.
- Operational logs may include timestamps, durations, model identifier, status category, and correlation IDs only after security review. They must not contain content.
- Do not send crash dumps containing process memory until dump scrubbing and retention are approved.

### Permissions (document in README, check at startup)
- Windows microphone access must be enabled for desktop apps. Detect stream-open failures and provide an actionable path to Settings → Privacy & security → Microphone.
- No Accessibility or Input Monitoring permission exists on Windows. Raw Input and `SendInput` may still be restricted or flagged by GM endpoint policy/EDR; obtain approval rather than requesting administrator elevation.
- The application must run as the standard signed-in user. Do not require local admin rights.
- Show a persistent visible indicator whenever the microphone stream is active.

## Commands

```powershell
uv sync
uv run voice2text
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python -m voice2text.gesture
uv run python -m voice2text.hotkey
uv run python -m voice2text.recorder
uv run python -m voice2text.transcriber tests/fixtures/hello.wav
```

Hardware/OS adapters should have `if __name__ == "__main__":` manual-test entry points. Pure modules should be covered through pytest rather than debug-only code.

## Testing

- CI-safe tests: gesture timing, event routing, recorder buffer logic, transcript filtering, OAuth PKCE utilities, Glean response parsing with mocked HTTP, config validation, and model checksum validation.
- Gesture tests: hold/release emits local dictation; one tap expires silently; two taps emit exactly one `GLEAN_START`; third trigger down emits exactly one `GLEAN_STOP`; its release is consumed; and an accidental first tap followed by a hold emits local dictation.
- Test exact threshold boundaries, duplicate make/break events, auto-repeat, unrelated keys, timeout cancellation, and maximum Glean duration.
- Windows integration tests: Raw Input emits only configured-trigger transitions; clipboard text is restored; injected paste has balanced key-down/key-up events; DPAPI round-trips only for the current user.
- Manual tests: microphone privacy denial, default-device change, first-word clipping, Teams/Outlook/browser/Office paste behavior, lock/unlock, sleep/resume, remote desktop, and EDR behavior.
- Live Glean tests require the registered test client and a non-production test plan. Verify permission trimming with two users who have intentionally different document access.
- Do not claim end-to-end success for any untested hardware, endpoint policy, or live tenant operation.

## Code style

- Type hints everywhere; `ruff` for lint + format (`uv run ruff check`, `uv run ruff format`).
- Keep OS boundaries narrow: `GestureStateMachine`, `WindowsTriggerListener`, `Recorder`, `Transcriber`, `WindowsPaster`, `OAuthClient`, `GleanClient`, and `Overlay`. Depend on protocols/interfaces and inject implementations from `main.py`.
- Never import Win32-only modules from pure logic modules. Tests for pure logic must run without a desktop session.
- Log with `logging`, not print (except the startup banner). One `--verbose` flag flips to DEBUG.
- No global mutable state outside class instances.

## Milestones (build in this order)

1. **Skeleton** — package layout, configuration, protocols, ruff, pytest, and Windows-focused README.
2. **Gesture** — pure state machine and exhaustive timing tests.
3. **Windows input** — Raw Input adapter that reports only Right Ctrl transitions. Manual test on this Windows machine and security review of the approach.
4. **Recorder** — WASAPI recording active only during capture. Manual playback test and first-frame latency measurement.
5. **Transcriber** — resident/warmed CPU model, fixture test, short-input rejection, and model benchmark on representative hardware.
6. **Paster** — Win32 clipboard, Ctrl+V, and plain-text restoration. Manual test in GM-standard applications.
7. **Mock Glean UX** — double-tap/third-tap flow, overlay, streamed fake answer, citations, cancellation, and error states.
8. **Glean authentication/API** — admin-registered OAuth client, DPAPI token storage, Chat API streaming, and permission tests.
9. **Integration** — queues, lifecycle, tray behavior, device changes, lock/unlock, startup checks, and full end-to-end test.
10. **Enterprise packaging** — x64 one-folder executable, signed binaries and installer, managed model payload, SBOM, vulnerability/license scan, and Intune/SCCM pilot.

Do not start a milestone until the previous one's manual test passes. Keep PRs to one milestone each.

For packaging, prefer a signed one-folder build over a self-extracting one-file executable: one-file startup extraction is slower and can look suspicious to EDR. Updates must come through GM software distribution, never an in-app updater.

## Known future work (do not build unless asked)

- Streaming partial inference while the key is held (cuts perceived latency).
- VAD-based silence trimming.
- Device-specific OEM Fn support after GM hardware validation.
- Alternative Windows acceleration backends behind the same `Transcriber` interface.
- A richer tray/settings UI.
- Custom vocabulary / initial-prompt biasing for names and jargon.
- macOS support; it remains a separate branch/product target.
