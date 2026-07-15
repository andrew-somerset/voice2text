from __future__ import annotations

import pytest

from voice2text.config import TriggerConfig
from voice2text.hotkey import RI_KEY_BREAK, RI_KEY_E0, TriggerFilter


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
    assert down.timestamp_ns == 3
    assert up is not None and up.is_down is False
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


def test_filter_rejects_negative_timestamp() -> None:
    trigger_filter = TriggerFilter()

    with pytest.raises(ValueError, match="cannot be negative"):
        trigger_filter.process(make_code=0x1D, flags=RI_KEY_E0, timestamp_ns=-1)
