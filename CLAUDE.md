# CLAUDE.md — voice2text GM Windows client

Windows 11 local voice dictation plus a secure voice route into Glean. Hold a configurable **trigger key**, speak, and release to paste a local transcript into the focused application. Double-tap the trigger to start Ask Glean recording, then tap it a third time to stop and submit. Raw audio and speech recognition remain on-device; only the final Ask Glean query text is sent to GM's Glean tenant.

This file describes the Windows-first `gm_dev` branch. The personal macOS design remains on `main`; do not carry Quartz, AppKit, Metal, macOS permissions, or Apple packaging assumptions into this branch.

## Project status

Milestones 1–7 and the persistent local runtime are implemented on `gm_dev`: selectable trigger setup, standalone-key gesture logic, Raw Input, native-rate WASAPI capture with local resampling, checksum-verified resident Whisper, targeted Win32 paste, the compact Mac-style bar pill, and the network-free mock Glean overlay. Hardware checks passed on this Windows machine for Right Alt Raw Input, 48 kHz capture, live scalar metering, model load/warm-up, known-content transcription, and focused-control paste/restoration. Direct `WM_PASTE` delivery to a disposable native Win32 edit control succeeded with exact text. Isolated Milestone 8 primitives cover public-client OAuth Authorization Code + PKCE, strict metadata and loopback validation, current-user DPAPI refresh-token storage, and OAuth-backed Client Chat behind mock HTTP transport. The current automated baseline is 144 passing tests plus clean Ruff lint and formatting checks.

Milestone 8 is not live-validated or complete. Live Glean remains disabled until GM and Glean administrators approve a public/native Authorization Code + PKCE registration with no desktop client secret and provide a non-production permission-test plan. The default `main.py` route is local-only; OAuth and Chat are not connected to it. Do not describe the current build as GM-deployment-ready.

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
│   ├── hotkey.py        # Windows Raw Input adapter for trigger/chord signals
│   ├── trigger_settings.py # reviewed choices and atomic per-user persistence
│   ├── trigger_setup.py # installer/first-run trigger picker
│   ├── recorder.py      # WASAPI/sounddevice capture into a buffer
│   ├── recording_pill.py # compact recording indicator and scalar meter
│   ├── recording_test.py # bounded trigger/microphone/pill hardware test
│   ├── local_runtime.py # persistent local transcription and focused paste worker
│   ├── instance_lock.py # current-session duplicate-listener prevention
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
    ├── test_recording_pill.py
    ├── test_recording_test.py
    ├── test_local_runtime.py
    ├── test_instance_lock.py
    ├── test_trigger_settings.py
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
- **Transcription worker**: consumes immutable memory-only audio jobs, runs local Whisper, inserts text into the captured target, emits content-free success/no-speech/error categories, and zeros every audio array in `finally`.
- **Glean worker**: performs OAuth refresh and streams Chat API responses without blocking input or transcription.
- **UI thread**: owns the compact recording pill or the thinking/answer overlay. Cross-thread updates use immutable queue commands. The pill receives only a normalized volume scalar, never audio samples.

## Trigger interaction

Windows laptop Fn keys are commonly handled by keyboard firmware or OEM utilities and often never appear as a normal Windows key event. **Do not make Fn a requirement or advertise it as universally selectable.** The installer/first-run picker offers reviewed Raw Input identities for **Right Alt**, **Right Ctrl**, **Right Shift**, **F8**, and **F9**. The non-secret choice is stored under `%LOCALAPPDATA%\voice2text\settings.json`; managed configuration can override it. Right Ctrl remains the safest fallback because it usually has no standalone action, not a hardware requirement.

Trigger use is standalone-only by default. Wait `CHORD_GRACE_SECONDS = 0.08` after trigger-down before activating local capture. If any other key is pressed during that window, suppress the trigger without starting the microphone. If an unrelated make event arrives after capture starts, cancel provisional audio and consume the trigger release without transcription or paste. This permits Right Alt/AltGr and other normal combinations while retaining no unrelated key identity. The listener is deliberately passive, so Windows and applications may still perform their native Alt, Shift, function-key, or other shortcut behavior.

- **Local dictation**: hold the trigger, speak, release. Transcribe locally and paste into the focused application.
- **Start Ask Glean**: tap and release the trigger twice within `DOUBLE_TAP_WINDOW_SECONDS`. Start recording only after the second release, after clearing all provisional tap audio. Show an unmistakable "Ask Glean — recording" indicator and play a distinct start sound.
- **Stop Ask Glean**: press the trigger a third time. Confirm a standalone stop on release or after the 80 ms grace deadline. A chord during the grace window resumes Glean recording instead of submitting. After confirmation, transcribe locally, submit the final text, and change the indicator to "Thinking".
- A single short tap expires silently and never transcribes or pastes.
- At `GLEAN_MAX_RECORDING_SECONDS`, stop and require explicit confirmation rather than submitting unexpectedly.

Use configurable starting values of `CHORD_GRACE_SECONDS = 0.08`, `TAP_MAX_SECONDS = 0.25`, `DOUBLE_TAP_WINDOW_SECONDS = 0.35`, and `GLEAN_MAX_RECORDING_SECONDS = 120.0`. Measure with `time.monotonic_ns()` and tune on representative GM hardware.

| State | Input | Action | Next state |
|---|---|---|---|
| `IDLE` | Trigger down | Start standalone-key grace deadline | `FIRST_PRESS` |
| `FIRST_PRESS` | Grace deadline | Start provisional local recording | `FIRST_PRESS` |
| `FIRST_PRESS` | Chord before grace | Do not activate capture | `CHORD_SUPPRESSED` |
| `FIRST_PRESS` | Chord after grace | Cancel provisional capture | `CHORD_SUPPRESSED` |
| `CHORD_SUPPRESSED` | Trigger up | Consume release | `IDLE` |
| `FIRST_PRESS` | Trigger up after a hold | Stop and queue local transcription | `IDLE` |
| `FIRST_PRESS` | Trigger up after a short tap | Discard any provisional audio and start double-tap deadline | `WAITING_SECOND_TAP` |
| `WAITING_SECOND_TAP` | Deadline expires | Do nothing | `IDLE` |
| `WAITING_SECOND_TAP` | Trigger down before deadline | Start a new standalone-key grace deadline | `SECOND_PRESS` |
| `SECOND_PRESS` | Trigger up after a short tap | Discard tap audio and start a fresh Glean recording | `GLEAN_RECORDING` |
| `SECOND_PRESS` | Trigger up after a hold | Treat as local dictation after an accidental first tap | `IDLE` |
| `GLEAN_RECORDING` | Trigger down | Start standalone stop grace | `GLEAN_STOP_PRESS` |
| `GLEAN_STOP_PRESS` | Chord before grace | Suppress stop; resume after release | `GLEAN_CHORD_SUPPRESSED` |
| `GLEAN_STOP_PRESS` | Grace deadline or trigger up | Stop and queue transcription plus submission | `IDLE` or `GLEAN_STOP_PRESS` until release |
| `GLEAN_CHORD_SUPPRESSED` | Trigger up | Resume active Glean recording | `GLEAN_RECORDING` |

The gesture state machine is pure Python and accepts timestamped `DOWN`, `UP`, identity-free `CHORD`, and `TIMER` inputs. Platform code translates Windows input into those events. This separation makes every timing boundary testable without a keyboard.

## Critical implementation details

These Windows details are security and latency requirements. Do not substitute broad convenience libraries without review.

### Windows trigger input (hotkey.py)
- Prefer Win32 **Raw Input** through a hidden message-only window registered with `RIDEV_INPUTSINK`. Microsoft recommends Raw Input over a low-level keyboard hook for most monitoring cases.
- Do not use `WH_KEYBOARD_LL`, `pynput`, or the `keyboard` package in the baseline. A global hook sees every keystroke, resembles keylogger behavior to security tools, participates in a shared hook chain, and can be silently removed when its callback is slow.
- Raw Input also receives keyboard events broadly. Inspect only enough `RAWKEYBOARD` data to match the configured scan code and `RI_KEY_E0`/`RI_KEY_E1` flag. Outside a held trigger, immediately discard every non-trigger event. While chord suppression is enabled and the trigger is physically down, convert only the first unrelated make event into an identity-free `CHORD` marker. Never retain its make code, virtual key, text, device, timing history, or count, and never log it.
- Do not use `RIDEV_NOLEGACY`; the app is listen-only and must not suppress input to other applications.
- De-duplicate key auto-repeat and repeated make/break messages. Emit one transition only when the trigger state actually changes.
- If security review rejects background Raw Input, provide a lower-capability fallback based on `RegisterHotKey`; do not quietly replace it with a global hook.
- Treat OEM Fn support as optional future hardware validation. If a particular GM laptop emits Fn as raw HID, add an allowlisted device-specific mapping rather than making it universal behavior.
- The setup picker must not offer arbitrary broad key capture. Add choices only as reviewed scan-code and extended-flag pairs; this keeps installation from resembling a keystroke collector.

### Transcription (transcriber.py)
- Load the model **once at startup** and keep it resident. Per-utterance model loading costs 1–2s and destroys the latency budget.
- Run one dummy inference on approximately one second of silence at startup so allocations and native initialization occur before the first utterance.
- Use `language="en"`, join segment text, disable printing/progress, and benchmark `n_threads` rather than assuming every logical core is faster.
- Input contract at the `Transcriber` boundary: 16kHz mono float32 numpy array in [-1, 1]. The recorder converts a native WASAPI shared-mode rate to this contract locally; the transcriber itself must never resample or accept another rate.
- Drop utterances shorter than ~0.3s (accidental taps) — whisper hallucinates on near-empty audio ("Thank you." on silence is the classic failure). Also strip leading/trailing whitespace and discard results that are only punctuation.
- Production must load a locally managed model by path and verify its SHA-256 checksum before use. Never download a model at runtime on a GM endpoint.
- Record benchmark results by laptop model, CPU, Whisper model, thread count, utterance duration, and release-to-result latency. Do not claim the 700ms target until measured on representative hardware.

### Audio (recorder.py)
- Use `sounddevice.InputStream`, channels=1, dtype="float32", through WASAPI shared mode.
- Open and configure the stream object once, but activate it only during recording so Windows does not show the microphone as continuously in use. Measure key-down-to-first-frame latency and clipping on GM hardware.
- Capture at the default WASAPI device's native shared-mode rate. Convert to Whisper's 16kHz contract locally with the pinned `soxr` dependency using its high-quality mode; test output length, dtype, and signal integrity. Never silently pass non-16kHz audio to Whisper.
- Buffer as a list of numpy chunks appended in the callback; concatenate on stop.
- Compute only the latest normalized RMS meter scalar in the callback. Keep it in the recorder under the same lock, expose no waveform samples to UI code, and reset it on stop, cancel, or close.
- Keep audio in memory only. Zero/drop references after transcription and never write temporary WAV files outside explicit manual-test commands.

### Recording pill (recording_pill.py)
- Show a compact bottom-center always-on-top, non-activating pill whenever local or Ask Glean capture is active. Match the macOS visual language conceptually: nine white bars react to local voice level and nine orange bars shimmer during transcription; do not import AppKit implementation details.
- Animate only from the recorder's latest normalized `0..1` scalar and ephemeral GUI smoothing state. Do not transfer audio buffers, retain historical levels, infer content, or persist speech data.
- Tk owns all widgets and cleanup on its dedicated UI thread. Other threads may send only immutable commands through a queue.
- The `--test-recording-pill` route runs until the pill's close action or `Ctrl+C` by default; `--test-seconds` adds an optional positive bound. It exercises Raw Input, gesture deadlines, WASAPI, the scalar meter, cancellation, and cleanup without transcription, paste, Glean, or disk persistence. Completed test audio must be zeroed immediately. Safe terminal diagnostics may name only the configured trigger and `DOWN`, `UP`, or identity-free chord suppression.
- When automatic local paste is blocked, reuse the pill's Tk thread to show a temporary in-memory fallback card. Restore the prior clipboard first; place transcript text on the clipboard only after explicit **Copy**.

### Pasting (paster.py)
- Capture only opaque top-level and focused-child handles on trigger-down; never inspect or retain window title, application name, control text, or process content.
- Write UTF-16 text through `CF_UNICODETEXT`. Prefer targeted `WM_PASTE` for allowlisted standard edit-control classes, which avoids Right Alt menu-mode focus effects. For custom controls, cancel menu mode, restore the exact control, then synthesize a balanced Ctrl+V with `SendInput`.
- Save and restore only a previous plain-text clipboard value. Restoration must be asynchronous, generation-safe across rapid successive dictations, and conditional so newer external clipboard content is never overwritten. Do not claim preservation of rich formats in v1.
- Retry `OpenClipboard` briefly because another process may own it. Bound all retries and surface a failure instead of hanging.
- Add a short configurable clipboard-to-paste delay and restore delay; test Teams, Outlook, browsers, Office, and text editors.
- Hold one current-session named mutex for the local runtime so multiple listeners cannot create duplicate pastes.
- Never paste Glean answers automatically. Render answers and citations in the overlay and require an explicit copy action.

### Glean authentication and API (auth.py, glean_client.py)
- Use the official Glean Client Chat API over HTTPS. Do not automate a browser, scrape the Glean UI, embed a shared API token, or add a local MCP package for this narrow workflow.
- Authenticate each employee with OAuth Authorization Code + PKCE in the system browser. Use a loopback redirect on `127.0.0.1`, validate `state`, and never include a client secret in the desktop app.
- Request only the approved `CHAT` scope and `offline_access` only if GM allows refresh tokens. Glean must enforce the signed-in user's existing permissions.
- Protect refresh tokens for the current Windows user with DPAPI (`CryptProtectData`) or Windows Credential Manager. Never use machine-wide DPAPI protection, plaintext files, environment variables, logs, or source control for production tokens.
- Discover tenant OAuth endpoints from server metadata. Tenant URL, client ID, redirect URI, scopes, and optional chat application ID are managed configuration—not source constants.
- Send streaming requests to `POST /rest/api/v1/chat` with `stream: true`; never treat the OpenAPI `#stream` operation label as a transmissible URL fragment.
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
uv run voice2text --configure-trigger
uv run voice2text --configure-trigger right-alt
uv run voice2text --list-triggers
uv run voice2text --test-recording-pill
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

- CI-safe tests: gesture timing, event routing, recorder buffer and meter logic, Mac-style pill bar targets, test-route cancellation and zeroing, local worker queues, single-instance behavior, direct-control/SendInput paste selection, generation-safe clipboard restoration, transcript filtering, OAuth PKCE utilities, mocked Glean parsing, config validation, and model checksum validation.
- Gesture tests: hold/release emits local dictation; one tap expires silently; two taps emit exactly one `GLEAN_START`; a standalone third press emits exactly one `GLEAN_STOP`; an in-grace chord does not stop Glean; and an accidental first tap followed by a hold emits local dictation.
- Test exact threshold boundaries, standalone grace, pre-activation chords, late-chord cancellation, duplicate make/break events, auto-repeat, timeout cancellation, and maximum Glean duration.
- Windows integration tests: Raw Input emits only configured-trigger transitions plus identity-free chord markers; direct `WM_PASTE` inserts exact text into a disposable native edit control; the clipboard is restored; SendInput uses balanced key-down/key-up events; DPAPI round-trips only for the current user.
- Manual tests: each reviewed trigger on representative GM keyboards, Right Alt/AltGr native behavior, pill placement/scaling/volume response, microphone privacy denial, default-device change, first-word clipping, Teams/Outlook/browser/Office paste behavior, lock/unlock, sleep/resume, remote desktop, and EDR behavior.
- Live Glean tests require the registered test client and a non-production test plan. Verify permission trimming with two users who have intentionally different document access.
- Do not claim end-to-end success for any untested hardware, endpoint policy, or live tenant operation.

## Code style

- Type hints everywhere; `ruff` for lint + format (`uv run ruff check`, `uv run ruff format`).
- Keep OS boundaries narrow: `GestureStateMachine`, `WindowsTriggerListener`, `Recorder`, `Transcriber`, `WindowsPaster`, `OAuthClient`, `GleanClient`, and `Overlay`. Depend on protocols/interfaces and inject implementations from `main.py`.
- Never import Win32-only modules from pure logic modules. Tests for pure logic must run without a desktop session.
- Log with `logging`, not print (except the startup banner). One `--verbose` flag flips to DEBUG.
- No global mutable state outside class instances.

## Milestones (build in this order)

1. **Skeleton — complete** — package layout, configuration, atomic per-user trigger settings, first-run picker, protocols, ruff, pytest, and Windows-focused README.
2. **Gesture — complete** — pure state machine with standalone grace, chord suppression, and exhaustive timing tests.
3. **Windows input — original prototype validated; new choices require hardware checks** — Raw Input registration and Right Ctrl cleanup passed on this machine. Right Alt and other reviewed choices plus chord markers are unit-tested; representative GM keyboard, AltGr, native shortcut, and security review remain required.
4. **Recorder — prototype validated** — WASAPI capture is active only during recording; native 48 kHz capture and local 16 kHz conversion passed on this machine. Representative-device latency and clipping tests remain required.
5. **Transcriber — prototype validated** — resident/warmed CPU model, short-input rejection, checksum enforcement, and known-content benchmark passed. Representative GM laptop/model selection remains required.
6. **Paster — prototype validated** — Win32 clipboard, targeted native-control paste, balanced Ctrl+V fallback, generation-safe plain-text restoration, and the explicit-copy failure card pass. GM-standard application and EDR validation remain required.
7. **Mock Glean UX — complete** — compact voice-reactive bar pill, orange transcription shimmer, network-free streamed fake answer, citations, cancellation, errors, recording-limit state, and visible answer-overlay lifecycle pass. Representative display scaling still requires tuning.
8. **Glean authentication/API — isolated primitives complete; live validation blocked** — public-client PKCE, current-user DPAPI token storage, mocked Chat streaming, citation parsing, bounded responses, and sanitized error mapping pass. Admin registration and two-user permission tests remain required before runtime wiring.
9. **Integration — local route operational; broader lifecycle pending** — the default command wires Raw Input, gesture deadlines, memory-only recording, resident Whisper, targeted paste, non-activating UI, worker cleanup, and a single-instance lock. Glean routing, tray behavior, device changes, lock/unlock, and full GM-app testing remain pending.
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
