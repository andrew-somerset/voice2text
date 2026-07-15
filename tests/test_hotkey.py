from __future__ import annotations

import pytest

from voice2text.config import TriggerConfig
from voice2text.hotkey import (
    RI_KEY_BREAK,
    RI_KEY_E0,
    TriggerFilter,
    TriggerTransitionKind,
)


def test_filter_emits_only_configured_trigger_transitions() -> None:
    trigger_filter = TriggerFilter(TriggerConfig(scan_code=0x1D, extended=True))

    assert trigger_filter.process(make_code=0x2A, flags=0, timestamp_ns=1) is None
    assert trigger_filter.process(make_code=0x1D, flags=0, timestamp_ns=2) is None

    down = trigger_filter.process(make_code=0x1D, flags=RI_KEY_E0, timestamp_ns=3)
    up = trigger_filter.process(
        make_code=0x1D,
        flags=RI_KEY_E0 | RI_KEY_BREAK,
        timestamp_ns=4,
    )

    assert down is not None and down.is_down is True
    assert down.kind is TriggerTransitionKind.DOWN
    assert down.timestamp_ns == 3
    assert up is not None and up.is_down is False
    assert up.kind is TriggerTransitionKind.UP
    assert up.timestamp_ns == 4


def test_filter_deduplicates_auto_repeat_and_repeated_break() -> None:
    trigger_filter = TriggerFilter()

    assert trigger_filter.process(make_code=0x1D, flags=RI_KEY_E0, timestamp_ns=1)
    assert trigger_filter.process(make_code=0x1D, flags=RI_KEY_E0, timestamp_ns=2) is None
    assert trigger_filter.process(
        make_code=0x1D,
        flags=RI_KEY_E0 | RI_KEY_BREAK,
        timestamp_ns=3,
    )
    assert (
        trigger_filter.process(
            make_code=0x1D,
            flags=RI_KEY_E0 | RI_KEY_BREAK,
            timestamp_ns=4,
        )
        is None
    )


def test_filter_does_not_change_state_for_unrelated_keys() -> None:
    trigger_filter = TriggerFilter()
    trigger_filter.process(make_code=0x1D, flags=RI_KEY_E0, timestamp_ns=1)

    assert trigger_filter.process(make_code=0x30, flags=RI_KEY_BREAK, timestamp_ns=2) is None
    assert trigger_filter.is_down is True


def test_right_alt_is_a_distinct_selectable_trigger() -> None:
    trigger_filter = TriggerFilter(TriggerConfig(scan_code=0x38, extended=True))

    assert trigger_filter.process(make_code=0x38, flags=0, timestamp_ns=1) is None
    down = trigger_filter.process(make_code=0x38, flags=RI_KEY_E0, timestamp_ns=2)
    up = trigger_filter.process(
        make_code=0x38,
        flags=RI_KEY_E0 | RI_KEY_BREAK,
        timestamp_ns=3,
    )

    assert down is not None and down.kind is TriggerTransitionKind.DOWN
    assert up is not None and up.kind is TriggerTransitionKind.UP


def test_filter_emits_one_identity_free_chord_marker_while_trigger_is_down() -> None:
    trigger_filter = TriggerFilter(TriggerConfig(scan_code=0x38, extended=True))
    trigger_filter.process(make_code=0x38, flags=RI_KEY_E0, timestamp_ns=1)

    chord = trigger_filter.process(make_code=0x12, flags=0, timestamp_ns=2)

    assert chord is not None and chord.kind is TriggerTransitionKind.CHORD
    assert chord.timestamp_ns == 2
    assert not hasattr(chord, "make_code")
    with pytest.raises(ValueError, match="no trigger down/up state"):
        _ = chord.is_down
    assert trigger_filter.process(make_code=0x12, flags=0, timestamp_ns=3) is None
    assert trigger_filter.process(make_code=0x12, flags=RI_KEY_BREAK, timestamp_ns=4) is None
    release = trigger_filter.process(
        make_code=0x38,
        flags=RI_KEY_E0 | RI_KEY_BREAK,
        timestamp_ns=5,
    )
    assert release is not None and release.kind is TriggerTransitionKind.UP
    assert trigger_filter.is_down is False


def test_chord_markers_can_be_disabled_by_managed_configuration() -> None:
    trigger_filter = TriggerFilter(TriggerConfig(suppress_chords=False))
    trigger_filter.process(make_code=0x1D, flags=RI_KEY_E0, timestamp_ns=1)

    assert trigger_filter.process(make_code=0x20, flags=0, timestamp_ns=2) is None


def test_filter_rejects_negative_timestamp() -> None:
    trigger_filter = TriggerFilter()

    with pytest.raises(ValueError, match="cannot be negative"):
        trigger_filter.process(make_code=0x1D, flags=RI_KEY_E0, timestamp_ns=-1)
