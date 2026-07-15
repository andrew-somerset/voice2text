from __future__ import annotations

import pytest

from voice2text.config import TriggerConfig
from voice2text.gesture import (
    GestureEvent,
    GestureEventKind,
    GestureInput,
    GestureState,
    GestureStateMachine,
    InputKind,
)

MS = 1_000_000
SECOND = 1_000 * MS


def send(
    machine: GestureStateMachine,
    kind: InputKind,
    milliseconds: int,
) -> tuple[GestureEvent, ...]:
    return machine.handle(GestureInput(kind, milliseconds * MS))


def kinds(events: tuple[GestureEvent, ...]) -> tuple[GestureEventKind, ...]:
    return tuple(event.kind for event in events)


def immediate_machine() -> GestureStateMachine:
    """Use pre-grace timing for tests unrelated to chord suppression."""

    return GestureStateMachine(TriggerConfig(suppress_chords=False))


def start_glean(machine: GestureStateMachine) -> None:
    assert kinds(send(machine, InputKind.DOWN, 0)) == (GestureEventKind.LOCAL_START,)
    assert kinds(send(machine, InputKind.UP, 100)) == (GestureEventKind.LOCAL_CANCEL,)
    assert kinds(send(machine, InputKind.DOWN, 200)) == (GestureEventKind.LOCAL_START,)
    assert kinds(send(machine, InputKind.UP, 280)) == (
        GestureEventKind.LOCAL_CANCEL,
        GestureEventKind.GLEAN_START,
    )


def test_hold_release_emits_local_dictation() -> None:
    machine = immediate_machine()

    assert kinds(send(machine, InputKind.DOWN, 0)) == (GestureEventKind.LOCAL_START,)
    events = send(machine, InputKind.UP, 251)

    assert kinds(events) == (GestureEventKind.LOCAL_STOP,)
    assert events[0].duration_ns == 251 * MS
    assert machine.state is GestureState.IDLE


def test_exact_tap_threshold_is_a_short_tap() -> None:
    machine = immediate_machine()

    send(machine, InputKind.DOWN, 0)
    events = send(machine, InputKind.UP, 250)

    assert kinds(events) == (GestureEventKind.LOCAL_CANCEL,)
    assert machine.state is GestureState.WAITING_SECOND_TAP


def test_single_tap_expires_silently() -> None:
    machine = immediate_machine()

    send(machine, InputKind.DOWN, 0)
    send(machine, InputKind.UP, 100)
    assert machine.next_deadline_ns == 450 * MS

    assert send(machine, InputKind.TIMER, 449) == ()
    assert machine.state is GestureState.WAITING_SECOND_TAP
    assert send(machine, InputKind.TIMER, 450) == ()
    assert machine.state is GestureState.IDLE


def test_second_down_just_before_deadline_starts_second_press() -> None:
    machine = immediate_machine()

    send(machine, InputKind.DOWN, 0)
    send(machine, InputKind.UP, 100)
    events = send(machine, InputKind.DOWN, 449)

    assert kinds(events) == (GestureEventKind.LOCAL_START,)
    assert machine.state is GestureState.SECOND_PRESS


def test_second_down_at_deadline_becomes_a_new_first_press() -> None:
    machine = immediate_machine()

    send(machine, InputKind.DOWN, 0)
    send(machine, InputKind.UP, 100)
    events = send(machine, InputKind.DOWN, 450)

    assert kinds(events) == (GestureEventKind.LOCAL_START,)
    assert machine.state is GestureState.FIRST_PRESS


def test_double_tap_starts_exactly_one_glean_recording() -> None:
    machine = immediate_machine()

    start_glean(machine)

    assert machine.state is GestureState.GLEAN_RECORDING
    assert machine.next_deadline_ns == 120_280 * MS


def test_third_press_stops_glean_and_release_is_consumed() -> None:
    machine = immediate_machine()
    start_glean(machine)

    events = send(machine, InputKind.DOWN, 2_000)

    assert kinds(events) == (GestureEventKind.GLEAN_STOP,)
    assert events[0].duration_ns == 1_720 * MS
    assert machine.state is GestureState.GLEAN_STOP_PRESS
    assert send(machine, InputKind.UP, 2_050) == ()
    assert machine.state is GestureState.IDLE


def test_accidental_first_tap_then_hold_is_local_dictation() -> None:
    machine = immediate_machine()

    send(machine, InputKind.DOWN, 0)
    send(machine, InputKind.UP, 100)
    assert kinds(send(machine, InputKind.DOWN, 200)) == (GestureEventKind.LOCAL_START,)
    events = send(machine, InputKind.UP, 700)

    assert kinds(events) == (GestureEventKind.LOCAL_STOP,)
    assert events[0].duration_ns == 500 * MS
    assert machine.state is GestureState.IDLE


def test_duplicate_down_and_up_events_do_not_duplicate_commands() -> None:
    machine = immediate_machine()

    assert kinds(send(machine, InputKind.DOWN, 0)) == (GestureEventKind.LOCAL_START,)
    assert send(machine, InputKind.DOWN, 10) == ()
    assert kinds(send(machine, InputKind.UP, 300)) == (GestureEventKind.LOCAL_STOP,)
    assert send(machine, InputKind.UP, 301) == ()


def test_glean_max_duration_stops_without_automatic_submission() -> None:
    config = TriggerConfig(glean_max_recording_seconds=1.0, suppress_chords=False)
    machine = GestureStateMachine(config)
    start_glean(machine)

    assert send(machine, InputKind.TIMER, 1_279) == ()
    events = send(machine, InputKind.TIMER, 1_280)

    assert kinds(events) == (GestureEventKind.GLEAN_LIMIT_REACHED,)
    assert events[0].duration_ns == SECOND
    assert machine.state is GestureState.IDLE


def test_input_after_expired_glean_deadline_reports_limit_then_new_press() -> None:
    config = TriggerConfig(glean_max_recording_seconds=1.0, suppress_chords=False)
    machine = GestureStateMachine(config)
    start_glean(machine)

    events = send(machine, InputKind.DOWN, 1_300)

    assert kinds(events) == (
        GestureEventKind.GLEAN_LIMIT_REACHED,
        GestureEventKind.LOCAL_START,
    )
    assert machine.state is GestureState.FIRST_PRESS


def test_timestamp_regression_is_rejected() -> None:
    machine = immediate_machine()
    send(machine, InputKind.DOWN, 10)

    with pytest.raises(ValueError, match="non-decreasing"):
        send(machine, InputKind.UP, 9)


def test_negative_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        GestureInput(InputKind.DOWN, -1)


def test_chord_during_grace_never_starts_local_capture() -> None:
    machine = GestureStateMachine()

    assert send(machine, InputKind.DOWN, 0) == ()
    assert machine.next_deadline_ns == 80 * MS
    assert send(machine, InputKind.CHORD, 40) == ()
    assert machine.state is GestureState.CHORD_SUPPRESSED
    assert send(machine, InputKind.UP, 60) == ()
    assert machine.state is GestureState.IDLE


def test_chord_after_grace_cancels_provisional_local_capture() -> None:
    machine = GestureStateMachine()

    assert send(machine, InputKind.DOWN, 0) == ()
    assert kinds(send(machine, InputKind.TIMER, 80)) == (GestureEventKind.LOCAL_START,)
    events = send(machine, InputKind.CHORD, 120)

    assert kinds(events) == (GestureEventKind.LOCAL_CANCEL,)
    assert events[0].duration_ns == 120 * MS
    assert send(machine, InputKind.UP, 140) == ()
    assert machine.state is GestureState.IDLE


def test_standalone_hold_starts_after_grace_and_stops_normally() -> None:
    machine = GestureStateMachine()

    assert send(machine, InputKind.DOWN, 0) == ()
    assert kinds(send(machine, InputKind.TIMER, 80)) == (GestureEventKind.LOCAL_START,)
    events = send(machine, InputKind.UP, 300)

    assert kinds(events) == (GestureEventKind.LOCAL_STOP,)
    assert events[0].duration_ns == 300 * MS


def test_fast_double_tap_starts_glean_without_provisional_microphone_use() -> None:
    machine = GestureStateMachine()

    assert send(machine, InputKind.DOWN, 0) == ()
    assert send(machine, InputKind.UP, 50) == ()
    assert send(machine, InputKind.DOWN, 100) == ()
    events = send(machine, InputKind.UP, 150)

    assert kinds(events) == (GestureEventKind.GLEAN_START,)
    assert machine.state is GestureState.GLEAN_RECORDING


def test_trigger_chord_does_not_stop_active_glean_recording() -> None:
    machine = GestureStateMachine()
    send(machine, InputKind.DOWN, 0)
    send(machine, InputKind.UP, 50)
    send(machine, InputKind.DOWN, 100)
    send(machine, InputKind.UP, 150)

    assert send(machine, InputKind.DOWN, 1_000) == ()
    assert machine.state is GestureState.GLEAN_STOP_PRESS
    assert send(machine, InputKind.CHORD, 1_040) == ()
    assert machine.state is GestureState.GLEAN_CHORD_SUPPRESSED
    assert send(machine, InputKind.UP, 1_060) == ()
    assert machine.state is GestureState.GLEAN_RECORDING

    assert send(machine, InputKind.DOWN, 2_000) == ()
    events = send(machine, InputKind.UP, 2_050)
    assert kinds(events) == (GestureEventKind.GLEAN_STOP,)
    assert events[0].duration_ns == 1_900 * MS
    assert machine.state is GestureState.IDLE


def test_held_standalone_stop_is_confirmed_at_grace_deadline() -> None:
    machine = GestureStateMachine()
    send(machine, InputKind.DOWN, 0)
    send(machine, InputKind.UP, 50)
    send(machine, InputKind.DOWN, 100)
    send(machine, InputKind.UP, 150)

    send(machine, InputKind.DOWN, 1_000)
    assert machine.next_deadline_ns == 1_080 * MS
    events = send(machine, InputKind.TIMER, 1_080)

    assert kinds(events) == (GestureEventKind.GLEAN_STOP,)
    assert events[0].duration_ns == 930 * MS
    assert send(machine, InputKind.UP, 1_100) == ()
    assert machine.state is GestureState.IDLE
