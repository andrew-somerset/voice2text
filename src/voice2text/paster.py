"""Clipboard write + synthetic Cmd+V paste.

Writes the transcript to the general pasteboard, posts a synthetic Cmd+V to
the focused app, then restores the previous plain-string clipboard shortly
after on a background timer. Only the plain-string clipboard case is handled
in v1 — rich clipboard types are not preserved.

The paste keystroke is posted with a dedicated event source and an explicit
Command-key down/up wrapped around the V key (not merely the Command flag on
V) — this lands in more apps than the flag-only approach.

``paste`` reports back a :class:`PasteOutcome`. The one paste failure macOS
lets us detect reliably is **Secure Keyboard Entry**: when it is on (a terminal
setting, or forced by a focused password field / some security apps), the
system silently drops every synthetic keystroke. In that case we skip the
keystroke, leave the transcript on the clipboard for a manual paste, and let
the caller surface a "copy it yourself" window.

Manual-test only: requires Accessibility permission for the host app and a
real GUI session. Run ``python -m voice2text.paster`` and focus a text field.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass

from AppKit import NSPasteboard, NSPasteboardTypeString
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventSourceCreate,
    kCGEventFlagMaskCommand,
    kCGEventSourceStateCombinedSessionState,
    kCGHIDEventTap,
)

from voice2text import config

logger = logging.getLogger(__name__)

# macOS virtual keycodes.
_KEYCODE_V = 9
_KEYCODE_COMMAND = 0x37  # left Command (⌘)


def _load_secure_input_probe() -> ctypes.CDLL | None:
    """Load Carbon so we can call IsSecureEventInputEnabled; None off macOS."""
    try:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
        return carbon
    except OSError:
        return None


_CARBON = _load_secure_input_probe()


def secure_input_enabled() -> bool:
    """True if Secure Keyboard Entry is active (synthetic keystrokes are blocked)."""
    if _CARBON is None:
        return False
    try:
        return bool(_CARBON.IsSecureEventInputEnabled())
    except OSError:
        return False


@dataclass(frozen=True)
class PasteOutcome:
    """Result of a :meth:`Paster.paste` call."""

    pasted: bool  # True if the Cmd+V keystroke was posted
    reason: str  # empty when pasted; otherwise why the paste was skipped


class Paster:
    """Paste text into the focused app via clipboard + synthetic Cmd+V."""

    def __init__(self) -> None:
        self._pasteboard = NSPasteboard.generalPasteboard()
        # A shared event source produces keystrokes apps treat more like real
        # hardware than the NULL-source default.
        self._source = CGEventSourceCreate(kCGEventSourceStateCombinedSessionState)
        self._lock = threading.Lock()
        self._restore_timer: threading.Timer | None = None
        self._pending_original: str | None = None
        self._generation = 0

    def paste(self, text: str) -> PasteOutcome:
        """Paste ``text`` into the focused app.

        Returns immediately after Cmd+V is posted — it does not block for the
        restore delay. If Secure Keyboard Entry is active, the keystroke is
        skipped, the transcript is left on the clipboard, and the returned
        outcome reports the failure so the caller can offer a manual copy.
        """
        start = time.perf_counter()

        if not text:
            logger.debug("paste called with empty text; skipping")
            return PasteOutcome(pasted=False, reason="empty text")

        if secure_input_enabled():
            # We cannot type into the app, but we can still hand over the text.
            with self._lock:
                self._cancel_pending_restore_locked()
                self._pending_original = None  # keep the transcript on the clipboard
                self._write_clipboard(text)
            logger.warning(
                "Secure Keyboard Entry is on — macOS blocked the synthetic paste. "
                "The transcript is on the clipboard; paste it manually with Cmd+V."
            )
            return PasteOutcome(
                pasted=False,
                reason="Secure Keyboard Entry is on, so macOS blocked the paste.",
            )

        with self._lock:
            # Invalidate any pending restore so an old timer cannot clobber this
            # paste; carry the user's *original* clipboard across chained pastes.
            self._generation += 1
            generation = self._generation
            self._cancel_pending_restore_locked()

            if self._pending_original is not None:
                previous: str | None = self._pending_original
            else:
                previous = self._pasteboard.stringForType_(NSPasteboardTypeString)
            self._pending_original = previous

            self._write_clipboard(text)

        # Some apps race between the clipboard write and the paste keystroke.
        time.sleep(config.PASTE_DELAY_SECONDS)
        self._post_paste_keystroke()

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.debug("paste: Cmd+V posted %.1fms after entry (%d chars)", elapsed_ms, len(text))

        if previous is not None:
            timer = threading.Timer(
                config.CLIPBOARD_RESTORE_DELAY_SECONDS,
                self._restore_clipboard,
                args=(previous, generation),
            )
            timer.daemon = True
            with self._lock:
                if generation == self._generation:
                    self._restore_timer = timer
                    timer.start()

        return PasteOutcome(pasted=True, reason="")

    def _write_clipboard(self, text: str) -> None:
        """Overwrite the plain-string clipboard with ``text``."""
        self._pasteboard.clearContents()
        self._pasteboard.setString_forType_(text, NSPasteboardTypeString)

    def _post_paste_keystroke(self) -> None:
        """Post ⌘-down, V-down, V-up, ⌘-up to the HID event tap."""
        cmd_down = CGEventCreateKeyboardEvent(self._source, _KEYCODE_COMMAND, True)
        v_down = CGEventCreateKeyboardEvent(self._source, _KEYCODE_V, True)
        v_up = CGEventCreateKeyboardEvent(self._source, _KEYCODE_V, False)
        cmd_up = CGEventCreateKeyboardEvent(self._source, _KEYCODE_COMMAND, False)
        # Belt and suspenders: also stamp the Command flag onto the V events so
        # apps that read flags rather than key state still see ⌘V.
        CGEventSetFlags(v_down, kCGEventFlagMaskCommand)
        CGEventSetFlags(v_up, kCGEventFlagMaskCommand)
        for event in (cmd_down, v_down, v_up, cmd_up):
            CGEventPost(kCGHIDEventTap, event)

    def _cancel_pending_restore_locked(self) -> None:
        """Cancel a scheduled clipboard restore. Caller must hold ``self._lock``."""
        if self._restore_timer is not None:
            self._restore_timer.cancel()
            self._restore_timer = None

    def _restore_clipboard(self, previous: str, generation: int) -> None:
        """Put the pre-paste plain-string clipboard contents back."""
        with self._lock:
            if generation != self._generation:
                return  # superseded by a newer paste
            self._write_clipboard(previous)
            self._pending_original = None
            self._restore_timer = None
        logger.debug("clipboard restored (%d chars)", len(previous))


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")

    if secure_input_enabled():
        print(
            "NOTE: Secure Keyboard Entry is ON — this paste will be blocked by macOS.\n"
            "Turn it off (e.g. Terminal menu -> Secure Keyboard Entry) to test the real paste."
        )

    print("Pasting into the focused app in 3 seconds — focus a text field now...")
    time.sleep(3)

    sample = "Testing voice2text paste: hello, world — it's café-quality dictation!"
    result = Paster().paste(sample)
    print(f"outcome: pasted={result.pasted} reason={result.reason!r}")

    # Give the daemon restore timer time to fire before the process exits.
    time.sleep(config.CLIPBOARD_RESTORE_DELAY_SECONDS + 0.7)
    print("Done. If it pasted, your previous clipboard (plain text) has been restored.")
