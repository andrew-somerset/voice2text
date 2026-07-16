from __future__ import annotations

import ctypes

import pytest

from voice2text.paster import (
    _INPUT,
    VK_CONTROL,
    VK_V,
    FocusTarget,
    PasteMethod,
    PasteOutcome,
    WindowsPaster,
    paste_key_events,
)


class FakeClipboard:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.writes: list[str] = []
        self.clear_count = 0

    def read_text(self) -> str | None:
        return self.text

    def write_text(self, text: str) -> None:
        self.text = text
        self.writes.append(text)

    def clear(self) -> None:
        self.text = None
        self.clear_count += 1


class FakeFocusManager:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        direct: bool = False,
        valid: bool = True,
    ) -> None:
        self.failure = failure
        self.direct = direct
        self.valid = valid
        self.activations: list[FocusTarget] = []
        self.direct_pastes: list[FocusTarget] = []

    def capture(self) -> FocusTarget | None:
        return FocusTarget(foreground_window=42, focused_control=43)

    def validate(self, _target: FocusTarget) -> bool:
        if self.failure is not None:
            raise self.failure
        return self.valid

    def activate(self, target: FocusTarget) -> None:
        self.activations.append(target)
        if self.failure is not None:
            raise self.failure

    def paste_clipboard(self, target: FocusTarget) -> bool:
        self.direct_pastes.append(target)
        return self.direct


class FakeTimer:
    def __init__(self, _delay: float, callback: object, args: tuple[object, ...]) -> None:
        self.callback = callback
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback(*self.args)  # type: ignore[operator]


def test_key_sequence_is_balanced_and_ordered() -> None:
    events = paste_key_events()

    assert [(event.virtual_key, event.key_up) for event in events] == [
        (VK_CONTROL, False),
        (VK_V, False),
        (VK_V, True),
        (VK_CONTROL, True),
    ]


def test_send_input_structure_matches_windows_abi() -> None:
    expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(_INPUT) == expected_size


def test_paste_restores_previous_plain_text() -> None:
    clipboard = FakeClipboard("previous")
    sent: list[str] = []
    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=lambda: sent.append(clipboard.text or ""),
        sleep=lambda _seconds: None,
        restore_delay_seconds=0,
    )

    outcome = paster.paste("dictated text")

    assert outcome.pasted is True
    assert outcome.method is PasteMethod.SEND_INPUT
    assert sent == ["dictated text"]
    assert clipboard.writes == ["dictated text", "previous"]
    assert clipboard.text == "previous"


def test_targeted_standard_control_uses_direct_paste_without_send_input() -> None:
    clipboard = FakeClipboard("previous")
    focus = FakeFocusManager(direct=True)
    sent: list[str] = []
    paster = WindowsPaster(
        clipboard=clipboard,
        focus_manager=focus,
        send_paste=lambda: sent.append(clipboard.text or ""),
        sleep=lambda _seconds: None,
        restore_delay_seconds=0,
    )

    target = FocusTarget(foreground_window=42, focused_control=43)
    outcome = paster.paste("dictated text", target=target)

    assert focus.activations == []
    assert focus.direct_pastes == [target]
    assert sent == []
    assert outcome == PasteOutcome(True, PasteMethod.DIRECT_CONTROL)
    assert clipboard.text == "previous"


def test_targeted_custom_control_falls_back_to_send_input() -> None:
    clipboard = FakeClipboard("previous")
    focus = FakeFocusManager(direct=False)
    sent: list[str] = []
    paster = WindowsPaster(
        clipboard=clipboard,
        focus_manager=focus,
        send_paste=lambda: sent.append(clipboard.text or ""),
        sleep=lambda _seconds: None,
        restore_delay_seconds=0,
    )
    target = FocusTarget(foreground_window=42, focused_control=43)

    outcome = paster.paste("dictated text", target=target)

    assert outcome.pasted is True
    assert outcome.method is PasteMethod.SEND_INPUT
    assert focus.activations == [target]
    assert sent == ["dictated text"]


def test_target_failure_happens_before_clipboard_is_changed() -> None:
    clipboard = FakeClipboard("previous")
    focus = FakeFocusManager(valid=False)
    paster = WindowsPaster(
        clipboard=clipboard,
        focus_manager=focus,
        send_paste=lambda: None,
        sleep=lambda _seconds: None,
    )

    outcome = paster.paste(
        "dictated text",
        target=FocusTarget(foreground_window=42, focused_control=43),
    )

    assert outcome.pasted is False
    assert outcome.reason == "Original text control is no longer available"
    assert clipboard.writes == []
    assert clipboard.text == "previous"


def test_paste_clears_inserted_text_when_no_plain_text_existed() -> None:
    clipboard = FakeClipboard(None)
    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=lambda: None,
        sleep=lambda _seconds: None,
        restore_delay_seconds=0,
    )

    paster.paste("sensitive dictation")

    assert clipboard.text is None
    assert clipboard.clear_count == 1


def test_paste_does_not_overwrite_newer_clipboard_content() -> None:
    clipboard = FakeClipboard("previous")

    def external_change_after_paste() -> None:
        clipboard.text = "newer external content"

    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=external_change_after_paste,
        sleep=lambda _seconds: None,
        restore_delay_seconds=0,
    )

    paster.paste("dictated text")

    assert clipboard.text == "newer external content"
    assert clipboard.writes == ["dictated text"]


def test_paste_restores_clipboard_when_input_injection_fails() -> None:
    clipboard = FakeClipboard("previous")

    def fail_to_send() -> None:
        raise RuntimeError("synthetic input blocked")

    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=fail_to_send,
        sleep=lambda _seconds: None,
        restore_delay_seconds=0,
    )

    outcome = paster.paste("dictated text")

    assert outcome.pasted is False
    assert outcome.reason == "Windows blocked focused text insertion"
    assert clipboard.text == "previous"


def test_chained_pastes_cancel_stale_restore_and_preserve_original_clipboard() -> None:
    clipboard = FakeClipboard("original")
    timers: list[FakeTimer] = []

    def timer_factory(delay: float, callback: object, args: tuple[object, ...]) -> FakeTimer:
        timer = FakeTimer(delay, callback, args)
        timers.append(timer)
        return timer

    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=lambda: None,
        sleep=lambda _seconds: None,
        timer_factory=timer_factory,  # type: ignore[arg-type]
    )

    paster.paste("first")
    paster.paste("second")

    assert timers[0].cancelled is True
    timers[0].fire()
    assert clipboard.text == "second"
    timers[1].fire()
    assert clipboard.text == "original"


def test_close_restores_pending_clipboard_immediately() -> None:
    clipboard = FakeClipboard("original")
    timers: list[FakeTimer] = []

    def timer_factory(delay: float, callback: object, args: tuple[object, ...]) -> FakeTimer:
        timer = FakeTimer(delay, callback, args)
        timers.append(timer)
        return timer

    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=lambda: None,
        sleep=lambda _seconds: None,
        timer_factory=timer_factory,  # type: ignore[arg-type]
    )
    paster.paste("dictated")

    paster.close()

    assert timers[0].cancelled is True
    assert clipboard.text == "original"


@pytest.mark.parametrize("value", ["", "invalid\0text"])
def test_invalid_paste_text_is_rejected(value: str) -> None:
    paster = WindowsPaster(
        clipboard=FakeClipboard(None),
        send_paste=lambda: None,
        sleep=lambda _seconds: None,
        restore_delay_seconds=0,
    )

    with pytest.raises(ValueError):
        paster.paste(value)


def test_negative_delays_are_rejected() -> None:
    def send_paste() -> None:
        pass

    with pytest.raises(ValueError, match="cannot be negative"):
        WindowsPaster(
            clipboard=FakeClipboard(None),
            send_paste=send_paste,
            clipboard_delay_seconds=-0.1,
        )
