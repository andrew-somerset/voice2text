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

## Install for teammates

Give a teammate the
[Voice2Text 0.2.0 installer](https://github.com/andrew-somerset/voice2text/releases/download/v0.2.0/Voice2Text-Setup-0.2.0.exe).
They double-click it; no Python, Git, terminal, administrator rights, model download, or
environment variables are required. The installer includes the checksum-pinned local speech model
and opens a three-step setup window:

1. Voice2Text verifies the bundled local model.
2. It checks microphone access. If Windows blocks it, **Open Windows microphone settings** opens
	the exact privacy page and explains that both **Microphone access** and **Let desktop apps access
	your microphone** must be On. **Check again** verifies the change.
3. The user chooses Right Ctrl, Right Alt, Right Shift, F8, or F9. An optional live test confirms
	the key and microphone without transcribing, saving, or sending the test audio.

Selecting **Finish setup** starts Voice2Text immediately and registers it for every user sign-in.
It then runs without a console; the notification-area icon provides **Settings** and **Exit until
next sign-in**. The microphone is active only while the selected key is held.

The local pilot artifact is written to `dist\Voice2Text-Setup-0.2.0.exe`. It is not committed to
Git because it is a generated ~150 MB binary. Before broad team distribution, sign the installer
with an approved company code-signing certificate so Windows SmartScreen and endpoint policy can
identify its publisher.

## Developer quick start

From the repository root:

```powershell
uv sync --dev                                   # install the locked environment (uv installs once)
uv run voice2text --setup-model                 # download + checksum-verify the model (~148 MB, once)
uv run voice2text --configure-trigger right-ctrl # pick the key you hold to dictate
uv run voice2text                               # run it; hold the key, speak, release, it pastes
```

Also enable **Settings → Privacy & security → Microphone → Let desktop apps access your
microphone**. `--setup-model` is an explicit, one-time setup step: it stores the model under
`%LOCALAPPDATA%\voice2text\models\` and records its verified checksum, so no environment variables
are required and the resident app never downloads a model itself. If direct downloads are blocked on
a managed network, register a file fetched through an approved channel instead:

```powershell
uv run voice2text --setup-model --model-file C:\path\to\ggml-base.en.bin
```

See [Setup](#setup) for managed alternatives and [CLAUDE.md](CLAUDE.md) for the full design.

## Build the Windows installer

On a Windows build machine, install Inno Setup 6 once and prepare the reviewed model, then run:

```powershell
winget install --id JRSoftware.InnoSetup -e --source winget --scope user
uv run voice2text --setup-model
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
```

The build script recreates the locked environment, freezes a one-folder GUI application, bundles
the model only after verifying SHA-256, compiles the per-user installer, and prints the final
installer hash. The installer supports standard Inno Setup silent deployment switches for a later
Intune/SCCM pilot, while normal double-click installation always opens guided first-run setup.

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
- bounded Win32 UTF-16 clipboard access, opaque Windows UI Automation focus restoration, and
	balanced `SendInput` Ctrl+V delivery;
- generation-safe asynchronous restoration of the previous plain-text clipboard value;
- a compact bottom-center Mac-style nine-bar pill: white voice-reactive listening bars and an
	orange transcription shimmer, hosted as a non-activating window;
- current-session single-instance protection so duplicate listeners cannot duplicate a paste;
- console-free background launch with a real readiness handshake, clean named shutdown signal, and
	standard-user sign-in registration;
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
- a full production `WindowsFocusManager` + `WindowsPaster` flow captured the opaque UIA editor in
	Windows 11 Notepad, restored it from the worker thread, and produced an exact marker match through
	balanced `SendInput`;
- cross-process `WM_PASTE` was removed after Windows 11 Notepad returned API success without
	inserting text; the runtime no longer reports that false-success route;
- detached `pythonw.exe` launch reported ready, survived the launching terminal, and exited cleanly
	through the named shutdown event;
- current-user start-at-sign-in registration was installed and verified without model values,
	checksums, credentials, or content in its command;
- all 207 CI-safe tests, Ruff lint, and Ruff formatting checks;
- a guided microphone/trigger first-run workflow, notification-area settings control, frozen
	one-folder build, bundled-model verification, and per-user Windows installer.

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
- Glean runtime orchestration, device-change lifecycle, company code signing, security review, and
	enterprise Intune/SCCM pilot remain pending. The unsigned installer is for controlled local
	validation, not broad managed deployment.

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

Configure a locally managed Whisper model for real inference. The recommended path downloads the
reviewed default model once and records its verified checksum for you:

```powershell
uv run voice2text --list-models
uv run voice2text --setup-model
```

This stores `ggml-base.en.bin` under `%LOCALAPPDATA%\voice2text\models\`, verifies it against the
reviewed SHA-256 before installing it, and writes the path and checksum to
`%LOCALAPPDATA%\voice2text\model.json`. The resident app then loads that model automatically. If
direct downloads are blocked on a managed network, fetch `ggml-base.en.bin` through an approved
channel and register the existing file instead:

```powershell
uv run voice2text --setup-model --model-file C:\path\to\ggml-base.en.bin
```

A managed deployment can still point at a specific file with environment variables, which take
precedence over the recorded setup:

```powershell
$env:VOICE2TEXT_MODEL_PATH = "C:\managed-models\ggml-base.en.bin"
$env:VOICE2TEXT_MODEL_SHA256 = "<64-character reviewed SHA-256>"
```

Do not put model files, tokens, prompts, transcripts, or answers in source control.
The reviewed default is the official whisper.cpp `ggml-base.en.bin` artifact with SHA-256
`a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002`. Production selection still
requires benchmarking and security review on representative GM laptops; the resident application
never downloads a production model at runtime.

## Run local dictation

Start the persistent local app:

```powershell
uv run voice2text
```

Keep it running, click into any text box, hold the selected trigger, speak, and release. The small
bar pill reacts to voice volume, then changes to an orange shimmer during local transcription.
The app restores the exact focused element through Windows UI Automation, then sends a balanced
`Ctrl+V`. UIA is used only for opaque focus capture and `set_focus()`—the runtime never reads the
element's name, value, text, automation ID, or surrounding content. If focus restoration or input
is blocked, the prior clipboard is restored and a temporary card offers the transcript through an
explicit **Copy** button. Only one listener can run in the current Windows session.

For modifier triggers such as Right Alt, the runtime caches only the latest opaque focused-control
handles while idle and freezes that pre-key target when recording starts. Focus snapshots taken
after Windows enters Alt menu mode are rejected, so the paste returns to the editor instead of the
menu system.

## Keep it ready in the background

Install the current development build for this Windows user's sign-in and start it immediately:

```powershell
uv run voice2text --install-startup
```

The command waits until model warm-up, microphone setup, Raw Input registration, and the listener
all report ready before returning. It launches with `pythonw.exe`, so no terminal window remains.
Use these lifecycle commands when needed:

```powershell
uv run voice2text --background-status
uv run voice2text --stop-background
uv run voice2text --start-background
uv run voice2text --uninstall-startup
```

This prototype registers under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; it requires no
administrator rights and affects only the signed-in user. In this source checkout, the entry points
to the current virtual environment's `pythonw.exe`. Moving/deleting the repository or rebuilding
the virtual environment can invalidate it—rerun `--install-startup`. Enterprise deployment will
replace this development registration with the signed packaged executable distributed by GM.

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
uv run voice2text --background-status
uv run voice2text --start-background
uv run voice2text --stop-background
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
- Background lifecycle uses a current-session mutex plus named ready/stop events. Sign-in startup
	contains only the console-free module command; model configuration remains in reviewed user
	configuration and no content or credentials are placed in the startup entry.
- The Glean route will send only the final locally transcribed query over TLS.
- Glean answers will be displayed with citations and copied only by explicit user action—never
	pasted automatically.
- No content or token values are written to operational logs.
- Live Glean remains disabled until GM registers an OAuth Authorization Code + PKCE desktop client.

See [CLAUDE.md](CLAUDE.md) for the complete Windows architecture, security requirements, test plan,
and ordered milestones.
