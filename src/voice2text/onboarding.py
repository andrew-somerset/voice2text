"""Guided, standard-user first-run setup for the packaged Windows application."""

from __future__ import annotations

import hmac
import os
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Protocol

from voice2text.background import (
    LaunchResult,
    install_startup,
    launch_background,
    request_background_stop,
    uninstall_startup,
)
from voice2text.config import AppConfig
from voice2text.instance_lock import is_instance_running
from voice2text.recorder import Recorder, RecordingError
from voice2text.recording_test import RecordingTestOutcome, run_recording_pill_test
from voice2text.transcriber import sha256_file
from voice2text.trigger_settings import (
    TriggerChoice,
    save_trigger_settings,
    trigger_choice,
    trigger_choices,
)

_MICROPHONE_SETTINGS_URI = "ms-settings:privacy-microphone"
_WINDOW_WIDTH = 720
_WINDOW_HEIGHT = 550
_BG = "#f3f4f6"
_CARD = "#ffffff"
_TEXT = "#111827"
_MUTED = "#4b5563"
_BLUE = "#2563eb"
_BLUE_ACTIVE = "#1d4ed8"
_GREEN = "#047857"
_RED = "#b91c1c"
_BORDER = "#d1d5db"


class OnboardingError(RuntimeError):
    """The guided setup could not safely complete."""


class MicrophoneSurface(Protocol):
    def open(self) -> None: ...

    def start(self) -> None: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MicrophoneCheckResult:
    """Content-free microphone readiness result for the setup UI."""

    ready: bool
    message: str


@dataclass(frozen=True, slots=True)
class SetupCompletion:
    """Successful setup result shown without exposing configuration internals."""

    trigger: TriggerChoice
    launch_result: LaunchResult


def probe_microphone(
    config: AppConfig,
    *,
    recorder_factory: Callable[[], MicrophoneSurface] | None = None,
) -> MicrophoneCheckResult:
    """Activate and immediately stop the microphone to verify desktop-app access."""

    recorder = recorder_factory() if recorder_factory is not None else Recorder(config.audio)
    try:
        recorder.open()
        recorder.start()
        recorder.cancel()
    except RecordingError as exc:
        return MicrophoneCheckResult(False, str(exc))
    except Exception:
        return MicrophoneCheckResult(
            False,
            "Windows could not open the microphone. Check its connection and privacy settings.",
        )
    finally:
        with suppress(Exception):
            recorder.close()
    return MicrophoneCheckResult(True, "Microphone access is enabled and working.")


def open_microphone_settings(*, opener: Callable[[str], object] | None = None) -> None:
    """Open the Windows 11 microphone privacy page without changing any permission."""

    selected_opener = opener or getattr(os, "startfile", None)
    if selected_opener is None:
        raise OnboardingError("Windows microphone settings are unavailable on this system")
    try:
        selected_opener(_MICROPHONE_SETTINGS_URI)
    except OSError as exc:
        raise OnboardingError("Could not open Windows microphone settings") from exc


def verify_local_model(
    *,
    config_loader: Callable[[], AppConfig] = AppConfig.from_environment,
    checksum: Callable[[Any], str] = sha256_file,
) -> AppConfig:
    """Resolve and verify the configured or installer-bundled local model before setup continues."""

    config = config_loader()
    model_path = config.transcriber.model_path
    expected = config.transcriber.model_sha256
    if model_path is None or expected is None or not model_path.is_file():
        raise OnboardingError(
            "The local speech model is missing. Reinstall Voice2Text or ask your support contact "
            "for the approved installer."
        )
    actual = checksum(model_path)
    if not hmac.compare_digest(actual.lower(), expected.lower()):
        raise OnboardingError(
            "The local speech model did not pass its integrity check. Reinstall Voice2Text."
        )
    return config


def config_for_trigger(config: AppConfig, choice_id: str) -> AppConfig:
    """Build an in-memory test configuration for one reviewed trigger choice."""

    choice = trigger_choice(choice_id)
    return replace(
        config,
        trigger=replace(
            config.trigger,
            scan_code=choice.scan_code,
            extended=choice.extended,
            display_name=choice.display_name,
        ),
    )


def wait_for_listener_stop(
    *,
    running_probe: Callable[[], bool] = is_instance_running,
    timeout_seconds: float = 10.0,
) -> bool:
    """Wait briefly for the named listener mutex to be released after a clean stop request."""

    if timeout_seconds <= 0:
        raise ValueError("listener stop timeout must be positive")
    deadline = time.monotonic() + timeout_seconds
    while running_probe():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def stop_listener_for_setup(
    *,
    running_probe: Callable[[], bool] = is_instance_running,
    stop_request: Callable[[], bool] = request_background_stop,
    waiter: Callable[[], bool] | None = None,
) -> bool:
    """Stop an existing listener before a trigger test or configuration restart."""

    if not running_probe():
        return False
    if not stop_request():
        raise OnboardingError("The existing background listener could not be asked to stop")
    wait = waiter or (lambda: wait_for_listener_stop(running_probe=running_probe))
    if not wait():
        raise OnboardingError("The existing background listener did not stop in time")
    return True


def complete_setup(
    choice_id: str,
    *,
    running_probe: Callable[[], bool] = is_instance_running,
    stop_request: Callable[[], bool] = request_background_stop,
    waiter: Callable[[], bool] | None = None,
    save_trigger: Callable[..., object] = save_trigger_settings,
    startup_installer: Callable[[], None] = install_startup,
    startup_uninstaller: Callable[[], None] = uninstall_startup,
    launcher: Callable[[], LaunchResult] | None = None,
) -> SetupCompletion:
    """Persist the trigger, register sign-in startup, and launch the verified listener."""

    choice = trigger_choice(choice_id)
    stop_listener_for_setup(
        running_probe=running_probe,
        stop_request=stop_request,
        waiter=waiter,
    )
    save_trigger(choice.choice_id, suppress_chords=True)
    try:
        startup_installer()
        launch = launcher or (lambda: launch_background(timeout_seconds=60.0))
        result = launch()
    except Exception as exc:
        with suppress(Exception):
            startup_uninstaller()
        raise OnboardingError("Voice2Text could not be started in the background") from exc
    if result is LaunchResult.FAILED:
        startup_uninstaller()
        raise OnboardingError(
            "Voice2Text did not become ready. Check microphone access, then try again."
        )
    return SetupCompletion(trigger=choice, launch_result=result)


def _run_guided_hardware_test(config: AppConfig) -> RecordingTestOutcome:
    return run_recording_pill_test(
        config,
        duration_seconds=15.0,
        stop_after_capture=True,
    )


class FirstRunWizard:
    """Own the short Welcome → Microphone → Trigger → Ready Tk workflow."""

    def __init__(
        self,
        *,
        reconfigure: bool = False,
        model_verifier: Callable[[], AppConfig] = verify_local_model,
        microphone_probe: Callable[[AppConfig], MicrophoneCheckResult] = probe_microphone,
        settings_opener: Callable[[], None] = open_microphone_settings,
        hardware_test: Callable[[AppConfig], RecordingTestOutcome] = _run_guided_hardware_test,
        setup_finisher: Callable[[str], SetupCompletion] = complete_setup,
        listener_stopper: Callable[[], bool] = stop_listener_for_setup,
        background_restarter: Callable[[], LaunchResult] | None = None,
    ) -> None:
        try:
            import tkinter as tk
        except ImportError:
            raise OnboardingError("The Voice2Text setup window is unavailable") from None

        try:
            self._root = tk.Tk()
        except tk.TclError:
            raise OnboardingError("The Voice2Text setup window could not be opened") from None

        self._tk = tk
        self._reconfigure = reconfigure
        self._model_verifier = model_verifier
        self._microphone_probe = microphone_probe
        self._settings_opener = settings_opener
        self._hardware_test = hardware_test
        self._setup_finisher = setup_finisher
        self._listener_stopper = listener_stopper
        self._background_restarter = background_restarter or (
            lambda: launch_background(timeout_seconds=60.0)
        )
        self._config: AppConfig | None = None
        self._closed = False
        self._completed = False
        self._listener_stopped = False
        self._busy = False
        self._callbacks: queue.Queue[Callable[[], None]] = queue.Queue()
        self._selected_choice = tk.StringVar(value="right-ctrl")

        self._root.title("Voice2Text setup" if not reconfigure else "Voice2Text settings")
        self._root.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}")
        self._root.resizable(False, False)
        self._root.configure(bg=_BG)
        self._root.protocol("WM_DELETE_WINDOW", self._cancel)
        self._root.bind("<Escape>", lambda _event: self._cancel())

        self._shell = tk.Frame(self._root, bg=_BG, padx=34, pady=26)
        self._shell.pack(fill="both", expand=True)
        self._progress = tk.Label(
            self._shell,
            text="",
            bg=_BG,
            fg=_BLUE,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        )
        self._progress.pack(fill="x")
        self._title = tk.Label(
            self._shell,
            text="",
            bg=_BG,
            fg=_TEXT,
            font=("Segoe UI Semibold", 22),
            anchor="w",
        )
        self._title.pack(fill="x", pady=(5, 2))
        self._subtitle = tk.Label(
            self._shell,
            text="",
            bg=_BG,
            fg=_MUTED,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=640,
            anchor="w",
        )
        self._subtitle.pack(fill="x", pady=(0, 18))
        self._content = tk.Frame(self._shell, bg=_BG)
        self._content.pack(fill="both", expand=True)
        self._actions = tk.Frame(self._shell, bg=_BG)
        self._actions.pack(fill="x", pady=(18, 0))
        self._center()
        self._show_welcome()
        self._root.after(33, self._poll_callbacks)

    def run(self) -> bool:
        """Block until the wizard completes or is cancelled."""

        self._root.mainloop()
        return self._completed

    def _center(self) -> None:
        self._root.update_idletasks()
        x = max(0, (self._root.winfo_screenwidth() - _WINDOW_WIDTH) // 2)
        y = max(0, (self._root.winfo_screenheight() - _WINDOW_HEIGHT) // 2)
        self._root.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}+{x}+{y}")

    def _set_page(self, progress: str, title: str, subtitle: str) -> None:
        for parent in (self._content, self._actions):
            for child in parent.winfo_children():
                child.destroy()
        self._progress.configure(text=progress)
        self._title.configure(text=title)
        self._subtitle.configure(text=subtitle)

    def _card(self) -> Any:
        card = self._tk.Frame(
            self._content,
            bg=_CARD,
            highlightbackground=_BORDER,
            highlightthickness=1,
            padx=22,
            pady=18,
        )
        card.pack(fill="both", expand=True)
        return card

    def _button(
        self,
        parent: Any,
        text: str,
        command: Callable[[], None],
        *,
        primary: bool = False,
        state: str = "normal",
    ) -> Any:
        button = self._tk.Button(
            parent,
            text=text,
            command=command,
            state=state,
            fg="#ffffff" if primary else _TEXT,
            bg=_BLUE if primary else _CARD,
            activebackground=_BLUE_ACTIVE if primary else "#e5e7eb",
            activeforeground="#ffffff" if primary else _TEXT,
            disabledforeground="#9ca3af",
            relief="flat" if primary else "solid",
            borderwidth=1,
            padx=18,
            pady=8,
            font=("Segoe UI Semibold", 9),
            cursor="hand2",
        )
        return button

    def _body_label(
        self,
        parent: Any,
        text: str,
        *,
        color: str = _MUTED,
        bold: bool = False,
    ) -> Any:
        label = self._tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg=color,
            font=("Segoe UI Semibold" if bold else "Segoe UI", 10),
            justify="left",
            wraplength=590,
            anchor="w",
        )
        label.pack(fill="x", pady=4)
        return label

    def _show_welcome(self) -> None:
        self._set_page(
            "STEP 1 OF 3",
            "Welcome to Voice2Text",
            "Private, local dictation for any Windows text box.",
        )
        card = self._card()
        self._body_label(card, "Your voice stays on this PC", color=_TEXT, bold=True)
        self._body_label(
            card,
            "Audio is kept in memory only, transcribed locally, and discarded. Nothing is sent "
            "to a cloud transcription service.",
        )
        self._body_label(card, "Simple push-to-talk", color=_TEXT, bold=True)
        self._body_label(
            card,
            "Choose one key, hold it while speaking, and release it to insert the text where you "
            "were typing.",
        )
        self._body_label(card, "Always ready", color=_TEXT, bold=True)
        self._body_label(
            card,
            "Voice2Text starts automatically when you sign in and uses the microphone only while "
            "you hold your selected key.",
        )
        status = self._tk.StringVar(value="The local speech model will be verified next.")
        status_label = self._body_label(card, status.get())

        cancel = self._button(self._actions, "Cancel", self._cancel)
        cancel.pack(side="left")
        next_button = self._button(
            self._actions,
            "Get started",
            lambda: self._prepare_model(status, status_label, next_button),
            primary=True,
        )
        next_button.pack(side="right")

    def _prepare_model(self, status: Any, status_label: Any, button: Any) -> None:
        if self._busy:
            return
        self._busy = True
        button.configure(state="disabled")
        status.set("Verifying the included local speech model...")
        status_label.configure(textvariable=status, fg=_BLUE)

        def success(config: AppConfig) -> None:
            self._busy = False
            self._config = config
            self._selected_choice.set(self._saved_choice_id(config))
            self._show_microphone()

        def failure(exc: BaseException) -> None:
            self._busy = False
            status.set(str(exc) or "The local speech model could not be verified.")
            status_label.configure(textvariable=status, fg=_RED)
            button.configure(state="normal", text="Try again")

        self._run_async(self._model_verifier, success, failure)

    @staticmethod
    def _saved_choice_id(config: AppConfig) -> str:
        for choice in trigger_choices():
            if (
                choice.scan_code == config.trigger.scan_code
                and choice.extended == config.trigger.extended
            ):
                return choice.choice_id
        return "right-ctrl"

    def _show_microphone(self) -> None:
        self._set_page(
            "STEP 2 OF 3",
            "Enable microphone access",
            "Windows controls this privacy setting. Voice2Text can check it and open the exact "
            "Settings page, but cannot change it for you.",
        )
        card = self._card()
        self._body_label(card, "In Windows Settings, make sure both switches are On:", bold=True)
        self._body_label(card, "1. Microphone access", color=_TEXT)
        self._body_label(card, "2. Let desktop apps access your microphone", color=_TEXT)
        status = self._tk.StringVar(value="Checking microphone access...")
        status_label = self._body_label(card, status.get(), color=_BLUE, bold=True)

        settings_button = self._button(
            card,
            "Open Windows microphone settings",
            lambda: self._open_settings(status, status_label),
        )
        settings_button.pack(anchor="w", pady=(16, 5))
        retry_button = self._button(
            card,
            "Check again",
            lambda: self._check_microphone(status, status_label, next_button, retry_button),
        )
        retry_button.pack(anchor="w", pady=5)

        cancel = self._button(self._actions, "Cancel", self._cancel)
        cancel.pack(side="left")
        next_button = self._button(
            self._actions,
            "Continue",
            self._show_trigger,
            primary=True,
            state="disabled",
        )
        next_button.pack(side="right")
        self._root.after(
            150,
            lambda: self._check_microphone(
                status,
                status_label,
                next_button,
                retry_button,
            ),
        )

    def _check_microphone(
        self,
        status: Any,
        status_label: Any,
        next_button: Any,
        retry_button: Any,
    ) -> None:
        if self._busy or self._config is None:
            return
        self._busy = True
        next_button.configure(state="disabled")
        retry_button.configure(state="disabled")
        status.set("Checking microphone access...")
        status_label.configure(textvariable=status, fg=_BLUE)

        def success(result: MicrophoneCheckResult) -> None:
            self._busy = False
            status.set(result.message)
            status_label.configure(textvariable=status, fg=_GREEN if result.ready else _RED)
            retry_button.configure(state="normal")
            next_button.configure(state="normal" if result.ready else "disabled")

        def failure(_exc: BaseException) -> None:
            self._busy = False
            status.set("Windows could not check the microphone. Open Settings, then try again.")
            status_label.configure(textvariable=status, fg=_RED)
            retry_button.configure(state="normal")

        self._run_async(lambda: self._microphone_probe(self._config), success, failure)

    def _open_settings(self, status: Any, status_label: Any) -> None:
        try:
            self._settings_opener()
        except OnboardingError as exc:
            status.set(str(exc))
            status_label.configure(textvariable=status, fg=_RED)

    def _show_trigger(self) -> None:
        self._set_page(
            "STEP 3 OF 3",
            "Choose your recording key",
            "Hold this key while speaking, then release it to insert your words.",
        )
        card = self._card()
        descriptions = {choice.choice_id: choice.description for choice in trigger_choices()}
        description = self._tk.StringVar(value=descriptions[self._selected_choice.get()])

        def update_description() -> None:
            description.set(descriptions[self._selected_choice.get()])
            test_status.set("Optional: test the selected key before finishing.")
            test_status_label.configure(textvariable=test_status, fg=_MUTED)

        choices_frame = self._tk.Frame(card, bg=_CARD)
        choices_frame.pack(fill="x")
        for choice in trigger_choices():
            self._tk.Radiobutton(
                choices_frame,
                text=choice.display_name,
                value=choice.choice_id,
                variable=self._selected_choice,
                command=update_description,
                bg=_CARD,
                fg=_TEXT,
                activebackground=_CARD,
                font=("Segoe UI", 10),
                anchor="w",
                selectcolor=_CARD,
            ).pack(side="left", padx=(0, 18), pady=4)
        description_label = self._body_label(card, description.get())
        description_label.configure(textvariable=description)

        test_status = self._tk.StringVar(
            value="Optional: test the selected key before finishing. Test audio is discarded."
        )
        test_status_label = self._body_label(card, test_status.get())
        test_button = self._button(
            card,
            "Test selected key",
            lambda: self._test_trigger(test_status, test_status_label, test_button),
        )
        test_button.pack(anchor="w", pady=(12, 0))

        cancel = self._button(self._actions, "Cancel", self._cancel)
        cancel.pack(side="left")
        finish = self._button(
            self._actions,
            "Finish setup",
            lambda: self._finish_setup(finish),
            primary=True,
        )
        finish.pack(side="right")

    def _test_trigger(self, status: Any, status_label: Any, button: Any) -> None:
        if self._busy or self._config is None:
            return
        self._busy = True
        button.configure(state="disabled")
        choice = trigger_choice(self._selected_choice.get())
        status.set(
            f"Hold {choice.display_name}, speak briefly, then release. The test closes after one "
            "successful hold."
        )
        status_label.configure(textvariable=status, fg=_BLUE)
        test_config = config_for_trigger(self._config, choice.choice_id)

        def task() -> RecordingTestOutcome:
            stopped = self._listener_stopper()
            self._listener_stopped = self._listener_stopped or stopped
            return self._hardware_test(test_config)

        def success(outcome: RecordingTestOutcome) -> None:
            self._busy = False
            if outcome.capture_completed:
                status.set(f"{choice.display_name} and the microphone are working.")
                status_label.configure(textvariable=status, fg=_GREEN)
                button.configure(state="normal", text="Test again")
            else:
                status.set("No completed key hold was detected. Try again, or finish setup anyway.")
                status_label.configure(textvariable=status, fg=_RED)
                button.configure(state="normal", text="Try again")

        def failure(exc: BaseException) -> None:
            self._busy = False
            status.set(str(exc) or "The key test could not be completed.")
            status_label.configure(textvariable=status, fg=_RED)
            button.configure(state="normal", text="Try again")

        self._run_async(task, success, failure)

    def _finish_setup(self, button: Any) -> None:
        if self._busy:
            return
        self._busy = True
        button.configure(state="disabled", text="Starting Voice2Text...")
        choice_id = self._selected_choice.get()

        def success(result: SetupCompletion) -> None:
            self._busy = False
            self._listener_stopped = False
            self._show_success(result)

        def failure(exc: BaseException) -> None:
            self._busy = False
            button.configure(state="normal", text="Try again")
            self._show_finish_error(str(exc) or "Voice2Text could not be started.")

        self._run_async(lambda: self._setup_finisher(choice_id), success, failure)

    def _show_finish_error(self, message: str) -> None:
        error = self._tk.Label(
            self._actions,
            text=message,
            bg=_BG,
            fg=_RED,
            font=("Segoe UI", 9),
            wraplength=400,
            justify="left",
        )
        error.pack(side="right", padx=(0, 12))

    def _show_success(self, result: SetupCompletion) -> None:
        self._set_page(
            "SETUP COMPLETE",
            "Voice2Text is ready",
            "It is running in the background now and will start automatically whenever you "
            "sign in.",
        )
        card = self._card()
        self._body_label(card, "Microphone access verified", color=_GREEN, bold=True)
        self._body_label(
            card,
            f"Recording key: {result.trigger.display_name}",
            color=_GREEN,
            bold=True,
        )
        self._body_label(card, "Start at sign-in enabled", color=_GREEN, bold=True)
        self._body_label(
            card,
            f"Try it now: click any text box, hold {result.trigger.display_name}, speak, and "
            "release. The small recording pill confirms when the microphone is active.",
            color=_TEXT,
        )
        self._body_label(
            card,
            "Use the Voice2Text tray icon or the Start menu shortcut to change your key later.",
        )
        finish = self._button(self._actions, "Done", self._done, primary=True)
        finish.pack(side="right")

    def _run_async(
        self,
        task: Callable[[], Any],
        success: Callable[[Any], None],
        failure: Callable[[BaseException], None],
    ) -> None:
        def worker() -> None:
            try:
                result = task()
            except BaseException as exc:
                error = exc
                self._post(lambda: failure(error))
            else:
                self._post(lambda: success(result))

        threading.Thread(target=worker, name="voice2text-setup-task", daemon=True).start()

    def _post(self, callback: Callable[[], None]) -> None:
        if self._closed:
            return
        self._callbacks.put(callback)

    def _poll_callbacks(self) -> None:
        if self._closed:
            return
        while True:
            try:
                callback = self._callbacks.get_nowait()
            except queue.Empty:
                break
            callback()
        self._root.after(33, self._poll_callbacks)

    def _done(self) -> None:
        self._completed = True
        self._close()

    def _cancel(self) -> None:
        if self._busy:
            return
        should_restart = self._listener_stopped and not self._completed
        self._close()
        if should_restart:
            threading.Thread(
                target=self._background_restarter,
                name="voice2text-setup-restore",
                daemon=False,
            ).start()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            self._root.destroy()


def run_first_run_wizard(*, reconfigure: bool = False) -> bool:
    """Show the packaged first-run or settings workflow and return whether it completed."""

    return FirstRunWizard(reconfigure=reconfigure).run()
