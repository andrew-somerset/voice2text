from __future__ import annotations

import numpy as np

from voice2text.gesture import GestureEvent, GestureEventKind
from voice2text.hotkey import TriggerTransitionKind
from voice2text.recording_test import RecordingTestController, _input_kind


class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.level = 0.64
        self.starts = 0
        self.cancels = 0
        self.stops = 0
        self.last_audio: np.ndarray | None = None

    def start(self) -> None:
        self.starts += 1
        self.is_recording = True

    def stop(self) -> np.ndarray:
        self.stops += 1
        self.is_recording = False
        self.last_audio = np.ones(16_000, dtype=np.float32)
        return self.last_audio

    def cancel(self) -> None:
        self.cancels += 1
        self.is_recording = False


class FakePill:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def show_local(self, trigger_name: str) -> None:
        self.calls.append(("local", trigger_name))

    def show_glean(self, trigger_name: str) -> None:
        self.calls.append(("glean", trigger_name))

    def show_complete(self, message: str = "Test audio discarded") -> None:
        self.calls.append(("complete", message))

    def show_error(self, message: str) -> None:
        self.calls.append(("error", message))

    def set_level(self, level: float) -> None:
        self.calls.append(("level", level))

    def hide(self) -> None:
        self.calls.append(("hide", None))


def event(kind: GestureEventKind, timestamp_ns: int = 1_000_000_000) -> GestureEvent:
    return GestureEvent(kind=kind, timestamp_ns=timestamp_ns)


def test_local_test_route_shows_meter_and_zeroes_discarded_audio() -> None:
    recorder = FakeRecorder()
    pill = FakePill()
    controller = RecordingTestController(
        trigger_name="Right Alt",
        recorder=recorder,
        pill=pill,
    )

    controller.handle((event(GestureEventKind.LOCAL_START),))
    controller.refresh(1_010_000_000)
    controller.handle((event(GestureEventKind.LOCAL_STOP),))

    assert recorder.starts == 1
    assert recorder.stops == 1
    assert ("local", "Right Alt") in pill.calls
    assert ("level", 0.64) in pill.calls
    assert ("complete", "1.0s captured - test audio discarded") in pill.calls
    assert recorder.last_audio is not None
    assert np.count_nonzero(recorder.last_audio) == 0


def test_local_cancel_discards_without_completion_feedback() -> None:
    recorder = FakeRecorder()
    pill = FakePill()
    controller = RecordingTestController(
        trigger_name="Right Alt",
        recorder=recorder,
        pill=pill,
    )

    controller.handle((event(GestureEventKind.LOCAL_START),))
    controller.handle((event(GestureEventKind.LOCAL_CANCEL),))

    assert recorder.cancels == 1
    assert recorder.stops == 0
    assert pill.calls[-1] == ("hide", None)
    assert all(name != "complete" for name, _value in pill.calls)


def test_glean_test_route_restarts_after_provisional_local_cancel() -> None:
    recorder = FakeRecorder()
    pill = FakePill()
    controller = RecordingTestController(
        trigger_name="Right Alt",
        recorder=recorder,
        pill=pill,
    )

    controller.handle((event(GestureEventKind.LOCAL_START),))
    controller.handle(
        (
            event(GestureEventKind.LOCAL_CANCEL),
            event(GestureEventKind.GLEAN_START),
        )
    )

    assert recorder.starts == 2
    assert recorder.cancels == 1
    assert pill.calls[-1] == ("glean", "Right Alt")


def test_completion_feedback_expires_and_abort_discards_active_audio() -> None:
    recorder = FakeRecorder()
    pill = FakePill()
    controller = RecordingTestController(
        trigger_name="F9",
        recorder=recorder,
        pill=pill,
    )

    controller.handle((event(GestureEventKind.LOCAL_START),))
    controller.handle((event(GestureEventKind.LOCAL_STOP, 2_000_000_000),))
    controller.refresh(2_899_999_999)
    assert pill.calls[-1][0] == "complete"
    controller.refresh(2_900_000_000)
    assert pill.calls[-1] == ("hide", None)

    controller.handle((event(GestureEventKind.LOCAL_START),))
    controller.abort()
    assert recorder.cancels == 1
    assert pill.calls[-1] == ("hide", None)


def test_transition_kind_mapping_includes_identity_free_chord() -> None:
    assert _input_kind(TriggerTransitionKind.DOWN).name == "DOWN"
    assert _input_kind(TriggerTransitionKind.UP).name == "UP"
    assert _input_kind(TriggerTransitionKind.CHORD).name == "CHORD"
