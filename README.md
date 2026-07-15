# voice2text — GM Windows prototype

Windows 11 local dictation with an optional, permission-aware Ask Glean route.

- Hold **Right Ctrl** to record local dictation; releasing it transcribes locally and will paste
	into the focused application.
- Double-tap **Right Ctrl** to start Ask Glean recording.
- Press it a third time to stop. Raw audio stays local; only the final query text may be sent.

The trigger is configurable because laptop firmware often prevents Windows from seeing the Fn key.
The current branch is `gm_dev`; the personal macOS design remains separate on `main`.

## Current status

Implemented and unit-tested:

- validated non-secret configuration;
- pure, timing-tested trigger gesture state machine;
- narrow Win32 Raw Input listener that discards every non-trigger key immediately;
- memory-only WASAPI capture at the device's native rate, with local high-quality conversion to
	Whisper's required 16 kHz mono float32 format;
- checksum-verified, resident `pywhispercpp` wrapper with short-input and punctuation filtering;
- bounded Win32 UTF-16 clipboard access and balanced `SendInput` Ctrl+V synthesis;
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
- all 84 CI-safe tests, Ruff lint, and Ruff formatting checks.

Still gated:

- Teams, Outlook, browser, and Office paste behavior still needs representative GM endpoint and EDR
	validation; the successful focused-control test is not a claim about those applications.
- Live Glean remains disabled until GM and Glean administrators approve a public/native OAuth
	Authorization Code + PKCE registration that does not put a client secret in the desktop app.
- The OAuth and Chat components have not been connected to the application runtime or exercised
	against a live GM tenant.
- Live Chat permission trimming requires a non-production plan and two approved test users with
	intentionally different source access.
- Full runtime orchestration, managed packaging, code signing, security review, and enterprise pilot
	remain pending.

## Setup

Install [uv](https://docs.astral.sh/uv/) once, then synchronize the locked environment:

```powershell
uv sync --dev
```

The prototype runs as the signed-in standard user. Do not elevate it. In Windows Settings, enable
**Privacy & security → Microphone → Let desktop apps access your microphone**.

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

## Safe checks

```powershell
uv run voice2text --check-config
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python -m voice2text.gesture
uv run python -m voice2text.hotkey --seconds 10
uv run python -m voice2text.recorder --seconds 3
uv run python -m voice2text.overlay --answer-seconds 4
```

The recorder command reports only duration, elapsed time, and signal peak; it does not write audio.
The hotkey command prints only configured Right Ctrl transitions and never records other keys.

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
- The Glean route will send only the final locally transcribed query over TLS.
- Glean answers will be displayed with citations and copied only by explicit user action—never
	pasted automatically.
- No content or token values are written to operational logs.
- Live Glean remains disabled until GM registers an OAuth Authorization Code + PKCE desktop client.

See [CLAUDE.md](CLAUDE.md) for the complete Windows architecture, security requirements, test plan,
and ordered milestones.
