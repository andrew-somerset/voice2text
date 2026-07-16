from __future__ import annotations

import pytest

from voice2text.recording_pill import (
    RecordingPillCommand,
    RecordingPillCommandKind,
    RecordingPillModel,
    RecordingPillStatus,
    _bar_targets,
)


def test_ready_pill_confirms_listener_and_selected_trigger() -> None:
    state = RecordingPillModel().apply(
        RecordingPillCommand(
            RecordingPillCommandKind.SHOW_READY,
            trigger_name="Right Alt",
        )
    )

    assert state.status is RecordingPillStatus.READY
    assert state.title == "Voice dictation ready"
    assert state.hint == "Hold Right Alt to record"


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


def test_local_transcription_and_paste_feedback_are_content_free() -> None:
    model = RecordingPillModel()

    transcribing = model.apply(RecordingPillCommand(RecordingPillCommandKind.SHOW_TRANSCRIBING))
    pasted = model.apply(RecordingPillCommand(RecordingPillCommandKind.SHOW_PASTED))
    no_speech = model.apply(RecordingPillCommand(RecordingPillCommandKind.SHOW_NO_SPEECH))

    assert transcribing.status is RecordingPillStatus.TRANSCRIBING
    assert transcribing.title == "Transcribing locally"
    assert pasted.title == "Text inserted"
    assert no_speech.title == "No speech detected"
    assert no_speech.hint == "Nothing was pasted"


def test_mac_style_bars_grow_with_voice_level() -> None:
    quiet = _bar_targets(RecordingPillStatus.LOCAL_RECORDING, 0.05, phase=0.7)
    speech = _bar_targets(RecordingPillStatus.LOCAL_RECORDING, 0.85, phase=0.7)

    assert len(quiet) == 9
    assert len(speech) == 9
    assert sum(speech) > sum(quiet) * 2
    assert all(0.05 <= height <= 1.0 for height in speech)


def test_transcribing_bars_shimmer_without_microphone_level() -> None:
    first = _bar_targets(RecordingPillStatus.TRANSCRIBING, 0.0, phase=0.0)
    second = _bar_targets(RecordingPillStatus.TRANSCRIBING, 0.0, phase=1.0)

    assert first != second
    assert len(set(first)) > 1


def test_paste_blocked_fallback_keeps_text_in_memory_and_clears_on_hide() -> None:
    private_text = "private local transcript"
    model = RecordingPillModel()

    fallback = model.apply(
        RecordingPillCommand(
            RecordingPillCommandKind.SHOW_PASTE_BLOCKED,
            message="Windows blocked focused text insertion",
            content=private_text,
        )
    )

    assert fallback.status is RecordingPillStatus.PASTE_BLOCKED
    assert fallback.content == private_text
    assert private_text not in repr(fallback)
    hidden = model.apply(RecordingPillCommand(RecordingPillCommandKind.HIDE))
    assert hidden.content == ""


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
