from __future__ import annotations

import ctypes
import sys
import threading

import pytest

from voice2text.paster import (
    _INPUT,
    VK_CONTROL,
    VK_V,
    FocusTarget,
    PasteMethod,
    PasteOutcome,
    WindowsPaster,
    _activate_uia_focus,
    _focus_target_from_gui_info,
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
        valid: bool = True,
    ) -> None:
        self.failure = failure
        self.valid = valid
        self.activations: list[FocusTarget] = []

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


class FakeUiaElement:
    def __init__(self) -> None:
        self.focus_count = 0

    def set_focus(self) -> None:
        self.focus_count += 1


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


def test_focus_target_uses_exact_valid_child_control() -> None:
    target = _focus_target_from_gui_info(
        10,
        11,
        is_window=lambda handle: handle in {10, 11},
        is_child=lambda parent, child: (parent, child) == (10, 11),
    )

    assert target == FocusTarget(10, 11)


def test_focus_target_redacts_opaque_uia_element_from_repr_and_comparison() -> None:
    private_element = object()
    target = FocusTarget(10, 11, uia_element=private_element)

    assert "object at" not in repr(target)
    assert target == FocusTarget(10, 11)


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation uses Windows COM")
def test_uia_activation_calls_only_focus_operation() -> None:
    element = FakeUiaElement()
    results: list[bool] = []

    thread = threading.Thread(target=lambda: results.append(_activate_uia_focus(element)))
    thread.start()
    thread.join(2)

    assert thread.is_alive() is False
    assert results == [True]
    assert element.focus_count == 1


def test_focus_target_uses_top_level_for_custom_ui_without_child_hwnd() -> None:
    target = _focus_target_from_gui_info(
        10,
        0,
        is_window=lambda handle: handle == 10,
        is_child=lambda _parent, _child: False,
    )

    assert target == FocusTarget(10, 10)


def test_focus_target_rejects_unrelated_child_by_using_current_top_level() -> None:
    target = _focus_target_from_gui_info(
        10,
        99,
        is_window=lambda handle: handle in {10, 99},
        is_child=lambda _parent, _child: False,
    )

    assert target == FocusTarget(10, 10)


def test_focus_target_rejects_invalid_foreground_window() -> None:
    target = _focus_target_from_gui_info(
        10,
        0,
        is_window=lambda _handle: False,
        is_child=lambda _parent, _child: False,
    )

    assert target is None


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


def test_targeted_control_restores_focus_and_uses_send_input() -> None:
    clipboard = FakeClipboard("previous")
    focus = FakeFocusManager()
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

    assert focus.activations == [target]
    assert sent == ["dictated text"]
    assert outcome == PasteOutcome(True, PasteMethod.SEND_INPUT)
    assert clipboard.text == "previous"


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
