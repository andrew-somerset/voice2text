from __future__ import annotations

import pytest

from voice2text.recording_pill import (
    RecordingPillCommand,
    RecordingPillCommandKind,
    RecordingPillModel,
    RecordingPillStatus,
)


def test_local_pill_names_selected_trigger_and_accepts_level() -> None:
    model = RecordingPillModel()

    shown = model.apply(
        RecordingPillCommand(
            RecordingPillCommandKind.SHOW_LOCAL,
            trigger_name="Right Alt",
        )
    )
    metered = model.apply(RecordingPillCommand(RecordingPillCommandKind.SET_LEVEL, level=0.72))

    assert shown.status is RecordingPillStatus.LOCAL_RECORDING
    assert shown.title == "Recording locally"
    assert "Right Alt" in shown.hint
    assert metered.level == 0.72


def test_glean_pill_uses_distinct_state_and_stop_instruction() -> None:
    state = RecordingPillModel().apply(
        RecordingPillCommand(
            RecordingPillCommandKind.SHOW_GLEAN,
            trigger_name="F9",
        )
    )

    assert state.status is RecordingPillStatus.GLEAN_RECORDING
    assert state.title == "Ask Glean recording"
    assert "Tap F9" in state.hint


def test_level_is_ignored_outside_recording_states() -> None:
    model = RecordingPillModel()
    model.apply(
        RecordingPillCommand(
            RecordingPillCommandKind.SHOW_COMPLETE,
            message="1.2s captured and discarded",
        )
    )

    state = model.apply(RecordingPillCommand(RecordingPillCommandKind.SET_LEVEL, level=1.0))

    assert state.status is RecordingPillStatus.COMPLETE
    assert state.level == 0.0


def test_hide_clears_meter_and_messages() -> None:
    model = RecordingPillModel()
    model.apply(
        RecordingPillCommand(
            RecordingPillCommandKind.SHOW_LOCAL,
            trigger_name="Right Alt",
        )
    )
    model.apply(RecordingPillCommand(RecordingPillCommandKind.SET_LEVEL, level=0.5))

    hidden = model.apply(RecordingPillCommand(RecordingPillCommandKind.HIDE))

    assert hidden == RecordingPillModel().state
    assert hidden.status is RecordingPillStatus.HIDDEN
    assert hidden.level == 0.0


@pytest.mark.parametrize("level", [-0.01, 1.01, float("inf"), float("nan")])
def test_command_rejects_invalid_meter_level(level: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        RecordingPillCommand(RecordingPillCommandKind.SET_LEVEL, level=level)


def test_command_rejects_multiline_message() -> None:
    with pytest.raises(ValueError, match="message is invalid"):
        RecordingPillCommand(
            RecordingPillCommandKind.SHOW_ERROR,
            message="private line\nsecond line",
        )
