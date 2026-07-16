# voice2text — GM Windows prototype

Windows 11 local dictation with an optional, permission-aware Ask Glean route.

- During setup, choose **Right Alt**, **Right Ctrl**, **Right Shift**, **F8**, or **F9**.
- Hold the selected trigger to record local dictation; releasing it transcribes locally and will
	paste into the focused application.
- Double-tap the trigger to start Ask Glean recording.
- Press it a third time to stop. Raw audio stays local; only the final query text may be sent.

An 80 ms standalone-key grace period rejects normal combinations before capture starts. If another
key arrives later while the trigger remains held, provisional capture is cancelled and never
transcribed or pasted. Fn is not a universal option because most laptop firmware never exposes it
to Windows; it can be enabled only after device-specific hardware validation.
The current branch is `gm_dev`; the personal macOS design remains separate on `main`.

## Current status

Implemented and unit-tested:

- validated non-secret configuration and an installer-style per-user trigger picker;
- pure, timing-tested trigger gesture state machine with standalone grace and chord suppression;
- narrow Win32 Raw Input listener that emits only trigger transitions and an identity-free `CHORD`
	marker while the trigger is held;
- memory-only WASAPI capture at the device's native rate, with local high-quality conversion to
	Whisper's required 16 kHz mono float32 format;
- checksum-verified, resident `pywhispercpp` wrapper with short-input and punctuation filtering;
- an integrated default local route: selected trigger → memory-only audio → local Whisper → the
	text control focused when recording began;
- bounded Win32 UTF-16 clipboard access, direct `WM_PASTE` for standard edit controls, and balanced
	`SendInput` Ctrl+V fallback for custom application controls;
- generation-safe asynchronous restoration of the previous plain-text clipboard value;
- a compact bottom-center Mac-style nine-bar pill: white voice-reactive listening bars and an
	orange transcription shimmer, hosted as a non-activating window;
- current-session single-instance protection so duplicate listeners cannot duplicate a paste;
- an in-memory paste-blocked fallback card with an explicit **Copy** action;
- network-free mock Glean streaming and a thread-owned overlay for recording, thinking, answers,
	citations, cancellation, errors, and recording-limit confirmation;
- isolated OAuth Authorization Code + PKCE primitives with strict same-tenant metadata validation,
	an IPv4 loopback callback, current-user DPAPI, and no desktop client-secret support;
- an OAuth-backed Glean Chat client that is disabled by default and mock-tested for newline-delimited
	streaming, citations, cancellation, bounded responses, one 401 refresh, and sanitized failures.

Validated on this Windows machine:

- Raw Input listener registration and cleanup;
- default WASAPI microphone capture at its native 48 kHz rate and local 16 kHz conversion;
- publisher-verified `ggml-base.en` model load, warm-up, and known-content transcription;
- 100% expected-phrase similarity on the known-content fixture, with 622.6 ms inference;
- focused-control paste injection and restoration of the previous plain-text clipboard value;
- the complete visible mock Glean overlay lifecycle, including clean UI-thread shutdown;
- current-user DPAPI round trips plus mocked OAuth and Chat transport behavior;
- a bounded recording-pill hardware test opened the selected Right Alt listener and WASAPI stream,
	exited successfully, and left no Python process behind;
- direct clipboard delivery to a disposable native Win32 edit control succeeded and its text
	matched exactly;
- all 144 CI-safe tests, Ruff lint, and Ruff formatting checks.

Still gated:

- Teams, Outlook, browser, and Office paste behavior still needs representative GM endpoint and EDR
	validation; the successful focused-control test is not a claim about those applications.
- Right Alt and the other new choices are unit-tested but still require physical-key and native
	application-behavior checks on representative GM keyboards. Right Alt may retain normal Windows
	or application Alt behavior because the listener intentionally does not suppress legacy input.
- The Right Alt trigger, recording pill, meter, and local inference are hardware-validated on this
	machine. End-to-end automatic insertion still needs representative checks in Teams, Outlook,
	browsers, Office, and GM-managed endpoint-policy conditions.
- Live Glean remains disabled until GM and Glean administrators approve a public/native OAuth
	Authorization Code + PKCE registration that does not put a client secret in the desktop app.
- The OAuth and Chat components have not been connected to the application runtime or exercised
	against a live GM tenant.
- Live Chat permission trimming requires a non-production plan and two approved test users with
	intentionally different source access.
- Glean runtime orchestration, tray/device lifecycle, managed packaging, code signing, security
	review, and enterprise pilot remain pending.

## Setup

Install [uv](https://docs.astral.sh/uv/) once, then synchronize the locked environment:

```powershell
uv sync --dev
```

The prototype runs as the signed-in standard user. Do not elevate it. In Windows Settings, enable
**Privacy & security → Microphone → Let desktop apps access your microphone**.

An installer or first-run flow opens the trigger picker with:

```powershell
uv run voice2text --configure-trigger
```

For managed or noninteractive setup, list and save a reviewed choice directly:

```powershell
uv run voice2text --list-triggers
uv run voice2text --configure-trigger right-alt
```

The non-secret choice is stored in `%LOCALAPPDATA%\voice2text\settings.json`. A managed deployment
can override it with `VOICE2TEXT_TRIGGER_CHOICE=right-alt`. Selecting Right Alt prevents dictation
for Right Alt combinations, including typical AltGr use, but does not block the combination's
normal behavior in Windows or the focused application.

Configure a locally managed Whisper model for real inference:

```powershell
$env:VOICE2TEXT_MODEL_PATH = "C:\managed-models\ggml-base.en.bin"
$env:VOICE2TEXT_MODEL_SHA256 = "<64-character reviewed SHA-256>"
```

Do not put model files, tokens, prompts, transcripts, or answers in source control.
The development benchmark used the official whisper.cpp `ggml-base.en.bin` artifact with SHA-256
`a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002`. Production selection still
requires benchmarking and security review on representative GM laptops; the application never
downloads a production model at runtime.

## Run local dictation

Start the persistent local app:

```powershell
uv run voice2text
```

Keep it running, click into any text box, hold the selected trigger, speak, and release. The small
bar pill reacts to voice volume, then changes to an orange shimmer during local transcription.
Standard Windows edit controls receive a direct targeted paste; custom controls use exact-focus
restoration plus balanced `Ctrl+V`. If Windows or endpoint policy blocks both paths, the prior
clipboard is restored and a temporary card offers the transcript through an explicit **Copy**
button. Only one listener can run in the current Windows session.

## Test the selected trigger and recording pill now

Run the hardware test from PowerShell. It remains active until explicitly stopped:

```powershell
uv run voice2text --test-recording-pill
```

A blue **Voice trigger ready** pill appears briefly on startup. The terminal also prints only safe
configured-trigger diagnostics such as `Right Alt: DOWN`, `Right Alt: UP`, or `combination
suppressed`; it never prints the unrelated key identity. For an optional time-limited check, add
`--test-seconds 60`.

Then:

1. Hold the selected trigger by itself for longer than 80 ms.
2. Speak while holding it. A small white-bar pill should appear at the bottom center; bar heights
	should follow your microphone level.
3. Release the trigger. This diagnostic discards the audio; the bars briefly change color, then hide.
4. Press the trigger together with another key. The combination should not remain recording; if
	the pill appeared after the grace period, it should immediately cancel and hide.
5. Double-tap the trigger to verify the pill's orange **Ask Glean recording** state, then tap once
	by itself to stop. This test does not contact Glean.

This mode deliberately performs no Whisper inference, clipboard operation, paste, file write, or
network request. Audio remains in memory, is zeroed immediately after each completed test hold, and
is discarded on cancellation or shutdown. Press `Ctrl+C` or click the pill's **X** to stop; an
optional `--test-seconds` value also ends it automatically.

## Safe checks

```powershell
uv run voice2text --check-config
uv run voice2text --list-triggers
uv run voice2text --test-recording-pill --test-seconds 60
uv run voice2text --test-local-dictation --test-seconds 60
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python -m voice2text.gesture
uv run python -m voice2text.hotkey --seconds 10
uv run python -m voice2text.recorder --seconds 3
uv run python -m voice2text.overlay --answer-seconds 4
```

The recorder command reports only duration, elapsed time, and signal peak; it does not write audio.
The hotkey command prints only the selected trigger's `DOWN`/`UP` transitions and an identity-free
`CHORD` suppression marker. It never records which unrelated key was pressed.

To repeat the focused paste check, focus a disposable text editor during the two-second countdown
and run:

```powershell
uv run python -m voice2text.paster "voice2text manual paste test"
```

Verify that the phrase appears once and that the previous plain-text clipboard value returns. Then
repeat in GM-standard Teams, Outlook, browser, Office, and text-editor fields. Rich clipboard
formats are not preserved in this prototype.

## Security boundary

- Local dictation makes no network request.
- Audio is held in memory only and dropped or zeroed after use.
- The recording meter exports only a normalized `0..1` scalar from the recorder to the UI thread;
	it never sends waveform samples to the pill and never stores level history.
- Outside explicit first-run selection, Raw Input retains no unrelated key identity, virtual key,
	text, device, or count. It emits at most one identity-free chord marker per trigger press.
- Focus targets contain only opaque top-level and child-control handles—never window titles, control
	text, application names, or typed content.
- Automatic paste failure restores the prior plain-text clipboard before offering an in-memory
	fallback. Transcript text enters the clipboard only after an explicit **Copy** action.
- The Glean route will send only the final locally transcribed query over TLS.
- Glean answers will be displayed with citations and copied only by explicit user action—never
	pasted automatically.
- No content or token values are written to operational logs.
- Live Glean remains disabled until GM registers an OAuth Authorization Code + PKCE desktop client.

See [CLAUDE.md](CLAUDE.md) for the complete Windows architecture, security requirements, test plan,
and ordered milestones.
