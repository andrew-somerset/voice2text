"""Explicit local dictation hardware route: trigger, Whisper, focused-window paste."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

from voice2text.config import AppConfig
from voice2text.gesture import (
    GestureEvent,
    GestureEventKind,
    GestureInput,
    GestureStateMachine,
    InputKind,
)
from voice2text.hotkey import (
    TriggerTransition,
    TriggerTransitionKind,
    WindowsTriggerListener,
)
from voice2text.instance_lock import SingleInstanceLock
from voice2text.paster import FocusTarget, PasteOutcome, WindowsFocusManager, WindowsPaster
from voice2text.recorder import FloatAudio, Recorder
from voice2text.recording_pill import RecordingPill
from voice2text.transcriber import Transcriber

LOGGER = logging.getLogger(__name__)
_LEVEL_REFRESH_NS = 33_000_000
_FEEDBACK_VISIBLE_NS = 1_200_000_000
_READY_VISIBLE_NS = 1_500_000_000


class LocalDictationError(RuntimeError):
    """The explicit local dictation route could not start or stop safely."""


class RuntimeRecorder(Protocol):
    @property
    def is_recording(self) -> bool: ...

    @property
    def level(self) -> float: ...

    def start(self) -> None: ...

    def stop(self) -> FloatAudio: ...

    def cancel(self) -> None: ...


class RuntimePill(Protocol):
    def show_ready(self, trigger_name: str) -> None: ...

    def show_local(self, trigger_name: str) -> None: ...

    def show_transcribing(self) -> None: ...

    def show_pasted(self) -> None: ...

    def show_no_speech(self) -> None: ...

    def show_error(self, message: str) -> None: ...

    def show_paste_blocked(self, text: str, reason: str) -> None: ...

    def set_level(self, level: float) -> None: ...

    def hide(self) -> None: ...


class RuntimeTranscriber(Protocol):
    def transcribe(self, audio: FloatAudio) -> str: ...


class RuntimePaster(Protocol):
    def paste(self, text: str, *, target: FocusTarget | None = None) -> PasteOutcome: ...


class RuntimeFocusManager(Protocol):
    def capture(self) -> FocusTarget | None: ...


class DictationResultKind(Enum):
    """Content-free completion categories returned by the transcription worker."""

    PASTED = auto()
    NO_SPEECH = auto()
    PASTE_BLOCKED = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class DictationResult:
    """One content-free result consumed by the main runtime thread."""

    kind: DictationResultKind
    text: str = field(default="", repr=False)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind is DictationResultKind.PASTE_BLOCKED:
            if not self.text or "\0" in self.text:
                raise ValueError("paste-blocked result requires valid in-memory text")
        elif self.text:
            raise ValueError("content-free dictation results cannot contain text")
        if len(self.reason) > 160 or any(character in "\r\n\0" for character in self.reason):
            raise ValueError("dictation result reason is invalid")


@dataclass(frozen=True, slots=True)
class DictationJob:
    """Memory-only audio and its opaque original foreground-window handle."""

    audio: FloatAudio
    target: FocusTarget


class LocalTranscriptionWorker:
    """Run local Whisper and focused-window paste off the input/UI threads."""

    def __init__(
        self,
        *,
        transcriber: RuntimeTranscriber,
        paster: RuntimePaster,
    ) -> None:
        self._transcriber = transcriber
        self._paster = paster
        self._jobs: queue.Queue[DictationJob | None] = queue.Queue()
        self._results: queue.Queue[DictationResult] = queue.Queue()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="voice2text-local-transcription",
            daemon=True,
        )
        self._thread.start()

    def submit(self, job: DictationJob) -> None:
        if self._thread is None or not self._thread.is_alive():
            job.audio.fill(0)
            raise LocalDictationError("local transcription worker is not running")
        self._jobs.put(job)

    def get_result_nowait(self) -> DictationResult | None:
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def wait_for_result(self, timeout_seconds: float = 2.0) -> DictationResult:
        """Wait for one content-free result in tests or explicit diagnostics."""

        if timeout_seconds <= 0:
            raise ValueError("result timeout must be positive")
        try:
            return self._results.get(timeout=timeout_seconds)
        except queue.Empty:
            raise TimeoutError("local transcription result did not arrive in time") from None

    def stop(self, timeout_seconds: float = 30.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._cancel.set()
        self._jobs.put(None)
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise TimeoutError("local transcription worker did not stop in time")
        self._thread = None

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                if self._cancel.is_set():
                    continue
                text = self._transcriber.transcribe(job.audio)
                if self._cancel.is_set():
                    continue
                if not text:
                    LOGGER.info("Local dictation result: no speech detected")
                    self._results.put(DictationResult(DictationResultKind.NO_SPEECH))
                    continue
                outcome = self._paster.paste(text, target=job.target)
                if not self._cancel.is_set() and outcome.pasted:
                    LOGGER.info(
                        "Local dictation result: text inserted via %s",
                        outcome.method.name.lower(),
                    )
                    self._results.put(DictationResult(DictationResultKind.PASTED))
                elif not self._cancel.is_set():
                    LOGGER.info("Local dictation result: automatic paste blocked")
                    self._results.put(
                        DictationResult(
                            DictationResultKind.PASTE_BLOCKED,
                            text=text,
                            reason=outcome.reason,
                        )
                    )
            except Exception:
                LOGGER.error("Local dictation result: transcription or focused paste failed")
                if not self._cancel.is_set():
                    self._results.put(DictationResult(DictationResultKind.ERROR))
            finally:
                job.audio.fill(0)


class LocalDictationController:
    """Route pure gesture commands to capture and content-free worker jobs."""

    def __init__(
        self,
        *,
        trigger_name: str,
        recorder: RuntimeRecorder,
        pill: RuntimePill,
        worker: LocalTranscriptionWorker,
    ) -> None:
        self._trigger_name = trigger_name
        self._recorder = recorder
        self._pill = pill
        self._worker = worker
        self._target: FocusTarget | None = None
        self._pending_jobs = 0
        self._hide_deadline_ns: int | None = None

    def remember_target(self, target: FocusTarget | None) -> None:
        """Keep only opaque top-level and focused-child handles captured on trigger-down."""

        self._target = target

    def show_ready(self, timestamp_ns: int) -> None:
        self._pill.show_ready(self._trigger_name)
        self._hide_deadline_ns = timestamp_ns + _READY_VISIBLE_NS

    def handle(self, events: tuple[GestureEvent, ...]) -> None:
        for event in events:
            if event.kind is GestureEventKind.LOCAL_START:
                self._start_local()
            elif event.kind is GestureEventKind.LOCAL_CANCEL:
                self._cancel_local()
            elif event.kind is GestureEventKind.LOCAL_STOP:
                self._finish_local(event.timestamp_ns)
            elif event.kind is GestureEventKind.GLEAN_START:
                self._cancel_local()
                self._pill.show_error("Ask Glean is not connected in local dictation mode")
                self._hide_deadline_ns = event.timestamp_ns + _FEEDBACK_VISIBLE_NS
            elif event.kind in {
                GestureEventKind.GLEAN_STOP,
                GestureEventKind.GLEAN_LIMIT_REACHED,
            }:
                self._cancel_local()

    def refresh(self, timestamp_ns: int) -> None:
        if self._recorder.is_recording:
            self._pill.set_level(self._recorder.level)
        result = self._worker.get_result_nowait()
        while result is not None:
            self._pending_jobs = max(0, self._pending_jobs - 1)
            if not self._recorder.is_recording and self._pending_jobs == 0:
                if result.kind is DictationResultKind.PASTED:
                    self._pill.show_pasted()
                elif result.kind is DictationResultKind.NO_SPEECH:
                    self._pill.show_no_speech()
                elif result.kind is DictationResultKind.PASTE_BLOCKED:
                    self._pill.show_paste_blocked(result.text, result.reason)
                else:
                    self._pill.show_error("Transcription or focused-window paste failed")
                if result.kind is not DictationResultKind.PASTE_BLOCKED:
                    self._hide_deadline_ns = timestamp_ns + _FEEDBACK_VISIBLE_NS
            result = self._worker.get_result_nowait()
        if self._hide_deadline_ns is not None and timestamp_ns >= self._hide_deadline_ns:
            self._hide_deadline_ns = None
            self._pill.hide()

    def abort(self) -> None:
        if self._recorder.is_recording:
            self._recorder.cancel()
        self._target = None
        self._hide_deadline_ns = None
        self._pill.hide()

    def _start_local(self) -> None:
        if self._recorder.is_recording:
            self._recorder.cancel()
        self._recorder.start()
        self._hide_deadline_ns = None
        self._pill.show_local(self._trigger_name)

    def _cancel_local(self) -> None:
        if self._recorder.is_recording:
            self._recorder.cancel()
        self._target = None
        self._hide_deadline_ns = None
        self._pill.hide()

    def _finish_local(self, timestamp_ns: int) -> None:
        if not self._recorder.is_recording:
            self._target = None
            return
        audio = self._recorder.stop()
        target, self._target = self._target, None
        if target is None:
            audio.fill(0)
            self._pill.show_error("No original text-box window was available")
            self._hide_deadline_ns = timestamp_ns + _FEEDBACK_VISIBLE_NS
            return
        self._worker.submit(DictationJob(audio=audio, target=target))
        self._pending_jobs += 1
        self._pill.show_transcribing()
        self._hide_deadline_ns = None


def run_local_dictation(
    config: AppConfig,
    *,
    duration_seconds: float | None = None,
) -> None:
    """Run local dictation until explicitly stopped, with no Glean network route."""

    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("local dictation duration must be positive")

    transitions: queue.Queue[TriggerTransition] = queue.Queue()
    stop_requested = threading.Event()
    pill = RecordingPill(on_cancel=stop_requested.set)
    recorder = Recorder(config.audio)
    focus_manager = WindowsFocusManager()
    paster = WindowsPaster(focus_manager=focus_manager)
    listener = WindowsTriggerListener(transitions.put, config.trigger)
    machine = GestureStateMachine(config.trigger)
    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        print("voice2text is already running; the existing listener remains active.")
        return

    pill_started = False
    listener_started = False
    worker: LocalTranscriptionWorker | None = None
    controller: LocalDictationController | None = None
    try:
        pill.start()
        pill_started = True
        print("Loading and warming the checksum-verified local Whisper model...", flush=True)
        transcriber = Transcriber(config.transcriber, config.audio)
        worker = LocalTranscriptionWorker(transcriber=transcriber, paster=paster)
        worker.start()
        controller = LocalDictationController(
            trigger_name=config.trigger.display_name,
            recorder=recorder,
            pill=pill,
            worker=worker,
        )
        recorder.open()
        listener.start()
        listener_started = True
        controller.show_ready(time.monotonic_ns())
        duration_label = (
            f"for {duration_seconds:g} seconds" if duration_seconds is not None else "until stopped"
        )
        print(
            f"Local dictation is listening for {config.trigger.display_name} {duration_label}.",
            flush=True,
        )
        print(
            "Focus a text box, hold the trigger, speak, and release. "
            "Press Ctrl+C or the pill's X to stop.",
            flush=True,
        )

        end_ns = (
            time.monotonic_ns() + round(duration_seconds * 1_000_000_000)
            if duration_seconds is not None
            else None
        )
        next_refresh_ns = time.monotonic_ns()
        while not stop_requested.is_set():
            now_ns = time.monotonic_ns()
            if end_ns is not None and now_ns >= end_ns:
                break
            gesture_deadline_ns = machine.next_deadline_ns
            wake_ns = next_refresh_ns if end_ns is None else min(end_ns, next_refresh_ns)
            if gesture_deadline_ns is not None:
                wake_ns = min(wake_ns, gesture_deadline_ns)
            try:
                transition = transitions.get(timeout=max(0.0, (wake_ns - now_ns) / 1_000_000_000))
            except queue.Empty:
                transition = None

            now_ns = time.monotonic_ns()
            while transition is not None:
                if transition.kind is TriggerTransitionKind.DOWN:
                    controller.remember_target(focus_manager.capture())
                controller.handle(
                    machine.handle(
                        GestureInput(
                            _input_kind(transition.kind),
                            transition.timestamp_ns,
                        )
                    )
                )
                try:
                    transition = transitions.get_nowait()
                except queue.Empty:
                    transition = None
            gesture_deadline_ns = machine.next_deadline_ns
            if gesture_deadline_ns is not None and now_ns >= gesture_deadline_ns:
                controller.handle(machine.handle(GestureInput(InputKind.TIMER, now_ns)))
            if now_ns >= next_refresh_ns:
                controller.refresh(now_ns)
                next_refresh_ns = now_ns + _LEVEL_REFRESH_NS
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        raise LocalDictationError(
            "Local dictation stopped safely after an operational error"
        ) from exc
    finally:
        try:
            if listener_started:
                listener.stop()
        finally:
            try:
                if controller is not None and pill_started:
                    controller.abort()
                elif recorder.is_recording:
                    recorder.cancel()
            finally:
                try:
                    if worker is not None:
                        worker.stop()
                finally:
                    try:
                        paster.close()
                    finally:
                        try:
                            recorder.close()
                        finally:
                            try:
                                if pill_started:
                                    pill.stop()
                            finally:
                                instance_lock.close()
    print("Local dictation stopped; no audio or transcript was persisted.")


def _input_kind(kind: TriggerTransitionKind) -> InputKind:
    mapping = {
        TriggerTransitionKind.DOWN: InputKind.DOWN,
        TriggerTransitionKind.UP: InputKind.UP,
        TriggerTransitionKind.CHORD: InputKind.CHORD,
    }
    return mapping[kind]
