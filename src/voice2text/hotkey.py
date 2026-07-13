"""Fn key press/release detection via a listen-only Quartz event tap.

The Fn key is a modifier: it never produces keyDown/keyUp events, only
``kCGEventFlagsChanged``. We install a session-level, listen-only event tap on
flagsChanged events and watch the ``kCGEventFlagMaskSecondaryFn`` bit. Only
transitions of that bit fire the callbacks, which both debounces repeats and
ignores flagsChanged noise from other modifiers (e.g. Fn+arrow page scrolls).

The tap callback must return in microseconds — macOS silently disables taps
with slow callbacks (``kCGEventTapDisabledByTimeout``). Handlers passed to
``HotkeyListener`` must therefore only flip flags or enqueue work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import Quartz

from voice2text import config

logger = logging.getLogger(__name__)

_PERMISSION_HELP = (
    "Could not create the keyboard event tap: macOS is blocking input monitoring for the "
    "app that launched this script (Terminal, iTerm, your IDE, ...).\n"
    "Fix it in System Settings → Privacy & Security → Input Monitoring: enable the launching "
    "app.\n"
    "Also enable it in System Settings → Privacy & Security → Accessibility.\n"
    "macOS never re-prompts after a denial — after changing the settings, fully quit and "
    "relaunch the app, then run this again."
)


class HotkeyListener:
    """Watches the Fn key and fires callbacks on press/release transitions."""

    def __init__(self, on_press: Callable[[], None], on_release: Callable[[], None]) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._fn_down: bool = False
        self._tap: object | None = None
        self._run_loop: object | None = None

    def install(self) -> None:
        """Create the event tap and add it to the calling thread's run loop.

        Does NOT run a run loop — the caller must already be driving one on this
        same thread (e.g. ``NSApplication``/``CFRunLoopRun``) for events to be
        delivered. Use this when embedding in a larger app; use :meth:`run` for
        standalone operation.

        Raises:
            PermissionError: if the tap cannot be created (missing Input
                Monitoring / Accessibility permission for the launching app).
        """
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
            self._tap_callback,
            None,
        )
        if tap is None:
            raise PermissionError(_PERMISSION_HELP)
        self._tap = tap

        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._run_loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        logger.info("Event tap installed; listening for Fn key transitions")

    def run(self) -> None:
        """Install the tap and run the CFRunLoop. Blocks the calling thread.

        Raises:
            PermissionError: if the tap cannot be created (see :meth:`install`).
        """
        self.install()
        try:
            Quartz.CFRunLoopRun()
        finally:
            logger.debug("Run loop exited")
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, False)
            self._run_loop = None

    def stop(self) -> None:
        """Stop the run loop so run() returns. Thread-safe; no-op if not running."""
        tap = self._tap
        if tap is not None:
            Quartz.CGEventTapEnable(tap, False)
        run_loop = self._run_loop
        if run_loop is not None:
            Quartz.CFRunLoopStop(run_loop)

    def _tap_callback(
        self, proxy: object, event_type: int, event: object, refcon: object
    ) -> object:
        """Quartz tap callback. Must do near-zero work and return the event."""
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            logger.warning("Event tap disabled by macOS (event type %s); re-enabling", event_type)
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
            return event

        if event_type == Quartz.kCGEventFlagsChanged:
            fn_now = bool(Quartz.CGEventGetFlags(event) & config.FN_FLAG_MASK)
            if fn_now != self._fn_down:
                self._fn_down = fn_now
                handler = self._on_press if fn_now else self._on_release
                try:
                    handler()
                except Exception:
                    logger.exception("Fn %s handler raised", "press" if fn_now else "release")
        return event


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    listener = HotkeyListener(
        on_press=lambda: print("Fn down"),
        on_release=lambda: print("Fn up"),
    )
    print("Listening for the Fn key. Hold and release it — Ctrl+C to quit.")
    print("(If Ctrl+C seems stuck, tap Fn once more so the run loop wakes up.)")
    try:
        listener.run()
    except KeyboardInterrupt:
        listener.stop()
        print("\nStopped.")
    except PermissionError as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
