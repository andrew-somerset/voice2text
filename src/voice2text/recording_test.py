"""Manual selected-trigger, microphone, and recording-pill hardware test."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
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
from voice2text.recorder import FloatAudio, Recorder
from voice2text.recording_pill import RecordingPill

_LEVEL_REFRESH_NS = 33_000_000
_COMPLETE_VISIBLE_NS = 900_000_000
_READY_VISIBLE_NS = 1_500_000_000


class RecordingPillTestError(RuntimeError):
    """The local hardware test could not start or finish safely."""


class RecorderSurface(Protocol):
    @property
    def is_recording(self) -> bool: ...

    @property
    def level(self) -> float: ...

    def start(self) -> None: ...

    def stop(self) -> FloatAudio: ...

    def cancel(self) -> None: ...


class PillSurface(Protocol):
    def show_ready(self, trigger_name: str) -> None: ...

    def show_local(self, trigger_name: str) -> None: ...

    def show_glean(self, trigger_name: str) -> None: ...

    def show_complete(self, message: str = "Test audio discarded") -> None: ...

    def show_error(self, message: str) -> None: ...

    def set_level(self, level: float) -> None: ...

    def hide(self) -> None: ...


class RecordingTestController:
    """Route gesture commands to a recorder and pill without transcription or persistence."""

    def __init__(
        self,
        *,
        trigger_name: str,
        recorder: RecorderSurface,
        pill: PillSurface,
    ) -> None:
        self._trigger_name = trigger_name
        self._recorder = recorder
        self._pill = pill
        self._hide_deadline_ns: int | None = None

    def handle(self, events: tuple[GestureEvent, ...]) -> None:
        """Apply ordered gesture commands and immediately zero completed test audio."""

        for event in events:
            if event.kind is GestureEventKind.LOCAL_START:
                self._start(local=True)
            elif event.kind is GestureEventKind.LOCAL_CANCEL:
                self._cancel()
            elif event.kind is GestureEventKind.LOCAL_STOP:
                self._finish(event.timestamp_ns)
            elif event.kind is GestureEventKind.GLEAN_START:
                self._start(local=False)
            elif event.kind is GestureEventKind.GLEAN_STOP:
                self._finish(event.timestamp_ns)
            elif event.kind is GestureEventKind.GLEAN_LIMIT_REACHED:
                self._finish(
                    event.timestamp_ns,
                    message="Recording limit reached - audio discarded",
                )

    def show_ready(self, timestamp_ns: int) -> None:
        """Confirm that the test listener is active before the first trigger press."""

        self._pill.show_ready(self._trigger_name)
        self._hide_deadline_ns = timestamp_ns + _READY_VISIBLE_NS

    def refresh(self, timestamp_ns: int) -> None:
        """Publish only the scalar meter level and expire transient completion feedback."""

        if self._recorder.is_recording:
            self._pill.set_level(self._recorder.level)
        if self._hide_deadline_ns is not None and timestamp_ns >= self._hide_deadline_ns:
            self._hide_deadline_ns = None
            self._pill.hide()

    def abort(self) -> None:
        """Stop any capture, discard all audio, and hide the pill."""

        if self._recorder.is_recording:
            self._recorder.cancel()
        self._hide_deadline_ns = None
        self._pill.hide()

    def _start(self, *, local: bool) -> None:
        if self._recorder.is_recording:
            self._recorder.cancel()
        self._recorder.start()
        self._hide_deadline_ns = None
        if local:
            self._pill.show_local(self._trigger_name)
        else:
            self._pill.show_glean(self._trigger_name)

    def _cancel(self) -> None:
        if self._recorder.is_recording:
            self._recorder.cancel()
        self._hide_deadline_ns = None
        self._pill.hide()

    def _finish(self, timestamp_ns: int, *, message: str | None = None) -> None:
        if not self._recorder.is_recording:
            return
        audio = self._recorder.stop()
        try:
            duration_seconds = audio.size / 16_000
            feedback = message or f"{duration_seconds:.1f}s captured - test audio discarded"
            self._pill.show_complete(feedback)
            self._hide_deadline_ns = timestamp_ns + _COMPLETE_VISIBLE_NS
        finally:
            audio.fill(0)


def run_recording_pill_test(
    config: AppConfig,
    *,
    duration_seconds: float | None = None,
    recorder_factory: Callable[[], Recorder] | None = None,
    pill_factory: Callable[[Callable[[], None]], RecordingPill] | None = None,
    listener_factory: Callable[
        [Callable[[TriggerTransition], None]],
        WindowsTriggerListener,
    ]
    | None = None,
) -> None:
    """Exercise the real trigger, microphone, and pill without inference, paste, or network."""

    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("recording pill test duration must be positive")

    transitions: queue.Queue[TriggerTransition] = queue.Queue()
    stop_requested = threading.Event()
    recorder = recorder_factory() if recorder_factory is not None else Recorder(config.audio)
    pill = (
        pill_factory(stop_requested.set)
        if pill_factory is not None
        else RecordingPill(on_cancel=stop_requested.set)
    )
    listener = (
        listener_factory(transitions.put)
        if listener_factory is not None
        else WindowsTriggerListener(transitions.put, config.trigger)
    )
    machine = GestureStateMachine(config.trigger)
    controller = RecordingTestController(
        trigger_name=config.trigger.display_name,
        recorder=recorder,
        pill=pill,
    )

    listener_started = False
    pill_started = False
    try:
        pill.start()
        pill_started = True
        recorder.open()
        listener.start()
        listener_started = True
        controller.show_ready(time.monotonic_ns())
        duration_label = (
            f"for {duration_seconds:g} seconds" if duration_seconds is not None else "until stopped"
        )
        print(
            f"Recording-pill test is listening for {config.trigger.display_name} {duration_label}.",
            flush=True,
        )
        print(
            f"Hold {config.trigger.display_name} and speak; release to discard the test audio. "
            "Key combinations should not open the pill. Press Ctrl+C to finish early."
        )

        end_ns = (
            time.monotonic_ns() + round(duration_seconds * 1_000_000_000)
            if duration_seconds is not None
            else None
        )
        next_level_ns = time.monotonic_ns()
        while not stop_requested.is_set():
            now_ns = time.monotonic_ns()
            if end_ns is not None and now_ns >= end_ns:
                break

            gesture_deadline_ns = machine.next_deadline_ns
            wake_ns = next_level_ns if end_ns is None else min(end_ns, next_level_ns)
            if gesture_deadline_ns is not None:
                wake_ns = min(wake_ns, gesture_deadline_ns)
            timeout_seconds = max(0.0, (wake_ns - now_ns) / 1_000_000_000)
            try:
                transition = transitions.get(timeout=timeout_seconds)
            except queue.Empty:
                transition = None

            now_ns = time.monotonic_ns()
            while transition is not None:
                print(_transition_message(config.trigger.display_name, transition.kind), flush=True)
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
            if now_ns >= next_level_ns:
                controller.refresh(now_ns)
                next_level_ns = now_ns + _LEVEL_REFRESH_NS
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if pill_started:
            with suppress(Exception):
                pill.show_error("Test stopped safely - check microphone and trigger access")
        raise RecordingPillTestError("Could not complete the recording pill test") from exc
    finally:
        try:
            if listener_started:
                listener.stop()
        finally:
            try:
                if pill_started:
                    controller.abort()
                elif recorder.is_recording:
                    recorder.cancel()
            finally:
                try:
                    recorder.close()
                finally:
                    if pill_started:
                        pill.stop()
    print("Recording-pill test finished; all captured audio was discarded.")


def _input_kind(kind: TriggerTransitionKind) -> InputKind:
    mapping = {
        TriggerTransitionKind.DOWN: InputKind.DOWN,
        TriggerTransitionKind.UP: InputKind.UP,
        TriggerTransitionKind.CHORD: InputKind.CHORD,
    }
    return mapping[kind]


def _transition_message(trigger_name: str, kind: TriggerTransitionKind) -> str:
    if kind is TriggerTransitionKind.CHORD:
        return f"{trigger_name}: combination suppressed"
    return f"{trigger_name}: {kind.name}"
