"""Best-effort learning from in-place corrections.

The idea: right after we paste a transcript, snapshot the focused text field
(via the macOS Accessibility API). Before the *next* dictation, read that field
again — if the user replaced one of the words we pasted with a different word,
treat it as a correction and learn ``pasted_word -> corrected_word``.

This is deliberately conservative and honest about its limits:

* It only reads text through Accessibility, which native apps expose but many
  Electron apps (e.g. VS Code) and some browser fields do **not**. Where the
  field is unreadable, nothing is learned — the deterministic substitution
  store still fixes terms once they are known by other means.
* It only learns small edits (<= a few words) to words we actually pasted, so
  editing your own surrounding text never trains it.
* Every learned correction is logged, and the ``vocabulary`` CLI can ``forget``
  a bad one — auto-learning's safety net is transparency + easy undo, not a
  perfect classifier.

``diff_corrections`` is a pure function (no macOS deps) so it is unit-tested on
CI; the Accessibility plumbing is guarded so this module imports anywhere.
"""

from __future__ import annotations

import difflib
import logging
import string
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

try:
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateSystemWide,
        kAXFocusedUIElementAttribute,
        kAXValueAttribute,
    )

    _AX_AVAILABLE = True
except Exception:  # pragma: no cover - only on non-macOS / missing framework
    _AX_AVAILABLE = False

_WORD_EDGE = string.punctuation + "…—"
_MAX_EDIT_SPAN = 3  # only learn replacements of at most this many words


def _core(token: str) -> str:
    """A token stripped of edge punctuation (so "control," matches "control")."""
    return token.strip(_WORD_EDGE)


def diff_corrections(before: str, after: str, pasted: str) -> list[tuple[str, str]]:
    """Find ``wrong -> right`` word replacements the user made to pasted text.

    ``before`` is the field right after we pasted; ``after`` is it now. Only
    small replacements whose original words all came from ``pasted`` are
    returned, so edits to the user's surrounding text are ignored.
    """
    pasted_cores = {_core(t).lower() for t in pasted.split() if _core(t)}
    if not pasted_cores:
        return []

    before_tokens = before.split()
    after_tokens = after.split()
    matcher = difflib.SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False)

    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        old = [_core(t) for t in before_tokens[i1:i2]]
        new = [_core(t) for t in after_tokens[j1:j2]]
        old = [t for t in old if t]
        new = [t for t in new if t]
        if not old or not new:
            continue
        if len(old) > _MAX_EDIT_SPAN or len(new) > _MAX_EDIT_SPAN:
            continue
        # Only learn if every replaced word came from what we pasted.
        if not all(t.lower() in pasted_cores for t in old):
            continue
        wrong = " ".join(old)
        right = " ".join(new)
        if wrong.lower() == right.lower():
            continue
        out.append((wrong, right))
    return out


class CorrectionWatcher:
    """Snapshots the focused field after a paste and learns edits to it.

    ``on_learn(wrong, right)`` is called for each detected correction. All AX
    access is best-effort and exception-guarded; ``available`` is False when
    the Accessibility API could not be loaded.
    """

    def __init__(self, on_learn: Callable[[str, str], None], enabled: bool = True) -> None:
        self._on_learn = on_learn
        self._enabled = enabled and _AX_AVAILABLE
        self._system = AXUIElementCreateSystemWide() if self._enabled else None
        self._lock = threading.Lock()
        self._pending: tuple[object, str, str] | None = None

    @property
    def available(self) -> bool:
        return self._enabled

    def _focused_value(self) -> tuple[object, str] | None:
        """The focused element and its text value, or None if unreadable."""
        err, element = AXUIElementCopyAttributeValue(
            self._system, kAXFocusedUIElementAttribute, None
        )
        if err != 0 or element is None:
            return None
        err, value = AXUIElementCopyAttributeValue(element, kAXValueAttribute, None)
        if err != 0 or not isinstance(value, str):
            return None
        return element, value

    def note_paste(self, pasted_text: str) -> None:
        """Snapshot the focused field after pasting ``pasted_text``."""
        if not self._enabled or not pasted_text:
            return
        try:
            snapshot = self._focused_value()
        except Exception:
            logger.debug("note_paste: AX read failed", exc_info=True)
            snapshot = None
        if snapshot is not None:
            copies = snapshot[1].count(pasted_text)
            if copies > 1:
                logger.warning(
                    "possible DOUBLE PASTE: focused field contains %d copies of the pasted text",
                    copies,
                )
            else:
                logger.debug("post-paste field holds %d copy of the pasted text", copies)
        with self._lock:
            self._pending = (snapshot[0], snapshot[1], pasted_text) if snapshot else None

    def check_for_correction(self) -> None:
        """Compare the snapshot to the field now and learn any corrections."""
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            return
        element, before, pasted = pending
        try:
            err, after = AXUIElementCopyAttributeValue(element, kAXValueAttribute, None)
        except Exception:
            logger.debug("check_for_correction: AX read failed", exc_info=True)
            return
        if err != 0 or not isinstance(after, str) or after == before:
            return
        for wrong, right in diff_corrections(before, after, pasted):
            logger.info("learned from your edit: %r -> %r", wrong, right)
            try:
                self._on_learn(wrong, right)
            except Exception:
                logger.exception("on_learn callback failed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"Accessibility available: {_AX_AVAILABLE}")
    demo = diff_corrections(
        before="Deploy it with cube control now.",
        after="Deploy it with kubectl now.",
        pasted="Deploy it with cube control now.",
    )
    print("demo diff:", demo)
