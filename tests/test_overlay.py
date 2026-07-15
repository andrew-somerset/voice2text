from __future__ import annotations

from voice2text.glean_client import Citation
from voice2text.overlay import (
    OverlayCommand,
    OverlayCommandKind,
    OverlayModel,
    OverlayStatus,
)


def test_local_and_glean_recording_states_are_unmistakable() -> None:
    model = OverlayModel()

    local = model.apply(OverlayCommand(OverlayCommandKind.SHOW_LOCAL_RECORDING))
    glean = model.apply(OverlayCommand(OverlayCommandKind.SHOW_GLEAN_RECORDING))

    assert local.status is OverlayStatus.LOCAL_RECORDING
    assert "Local" in local.title
    assert glean.status is OverlayStatus.GLEAN_RECORDING
    assert "Ask Glean" in glean.title


def test_streamed_answer_accumulates_and_finishes_with_citations() -> None:
    model = OverlayModel()
    citation = Citation(title="Mock source", url="https://example.invalid/source")

    model.apply(OverlayCommand(OverlayCommandKind.SHOW_THINKING))
    model.apply(OverlayCommand(OverlayCommandKind.APPEND_ANSWER, text="First "))
    partial = model.apply(OverlayCommand(OverlayCommandKind.APPEND_ANSWER, text="second"))
    complete = model.apply(
        OverlayCommand(OverlayCommandKind.COMPLETE_ANSWER, citations=(citation,))
    )

    assert partial.status is OverlayStatus.ANSWER
    assert partial.answer == "First second"
    assert complete.answer == "First second"
    assert complete.citations == (citation,)


def test_error_state_does_not_retain_previous_answer() -> None:
    model = OverlayModel()
    model.apply(OverlayCommand(OverlayCommandKind.APPEND_ANSWER, text="temporary answer"))

    state = model.apply(
        OverlayCommand(OverlayCommandKind.SHOW_ERROR, text="Request timed out safely")
    )

    assert state.status is OverlayStatus.ERROR
    assert state.answer == ""
    assert state.message == "Request timed out safely"


def test_limit_requires_an_explicit_decision() -> None:
    state = OverlayModel().apply(OverlayCommand(OverlayCommandKind.SHOW_LIMIT_CONFIRMATION))

    assert state.status is OverlayStatus.LIMIT_CONFIRMATION
    assert "Submit" in state.message
    assert "discard" in state.message


def test_hide_clears_in_memory_answer_and_citations() -> None:
    model = OverlayModel()
    citation = Citation(title="Mock source", url="https://example.invalid/source")
    model.apply(OverlayCommand(OverlayCommandKind.APPEND_ANSWER, text="temporary answer"))
    model.apply(OverlayCommand(OverlayCommandKind.COMPLETE_ANSWER, citations=(citation,)))

    hidden = model.apply(OverlayCommand(OverlayCommandKind.HIDE))

    assert hidden == model.state
    assert hidden.status is OverlayStatus.HIDDEN
    assert hidden.answer == ""
    assert hidden.citations == ()
