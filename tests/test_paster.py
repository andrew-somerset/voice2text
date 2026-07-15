from __future__ import annotations

import ctypes

import pytest

from voice2text.paster import _INPUT, VK_CONTROL, VK_V, WindowsPaster, paste_key_events


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
    )

    paster.paste("dictated text")

    assert sent == ["dictated text"]
    assert clipboard.writes == ["dictated text", "previous"]
    assert clipboard.text == "previous"


def test_paste_clears_inserted_text_when_no_plain_text_existed() -> None:
    clipboard = FakeClipboard(None)
    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=lambda: None,
        sleep=lambda _seconds: None,
    )

    paster.paste("sensitive dictation")

    assert clipboard.text is None
    assert clipboard.clear_count == 1


def test_paste_does_not_overwrite_newer_clipboard_content() -> None:
    clipboard = FakeClipboard("previous")
    sleeps = 0

    def external_change_after_paste(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            clipboard.text = "newer external content"

    paster = WindowsPaster(
        clipboard=clipboard,
        send_paste=lambda: None,
        sleep=external_change_after_paste,
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
    )

    with pytest.raises(RuntimeError, match="input blocked"):
        paster.paste("dictated text")

    assert clipboard.text == "previous"


@pytest.mark.parametrize("value", ["", "invalid\0text"])
def test_invalid_paste_text_is_rejected(value: str) -> None:
    paster = WindowsPaster(
        clipboard=FakeClipboard(None),
        send_paste=lambda: None,
        sleep=lambda _seconds: None,
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
