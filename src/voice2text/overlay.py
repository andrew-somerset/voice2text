"""On-screen UI: a Wispr-Flow-style listening pill and a fallback copy window.

Two floating, non-activating ``NSPanel``s that never steal focus from the app
you are dictating into:

* :class:`Overlay` — a small rounded pill at the bottom-center of the screen
  with a softly pulsing dot. ``show_listening`` while the Fn key is held,
  ``show_transcribing`` after release, ``hide`` when done.
* :class:`ResultWindow` — shown only when a paste could not be delivered (see
  ``config.RESULT_WINDOW_MODE``). Displays the transcript with a Copy button so
  the text is never lost, and auto-closes after a short linger.

AppKit rule: **every method here must run on the main thread.** The worker
thread marshals calls in via ``PyObjCTools.AppHelper.callAfter``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSMakePoint,
    NSMakeRect,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSScreen,
    NSTextField,
    NSTimer,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject
from Quartz import NSScreenSaverWindowLevel

from voice2text import config

logger = logging.getLogger(__name__)

_BG_WHITE = 0.13  # dark pill/card background
_BG_ALPHA = 0.94

# Waveform tuning. The level is mapped through a compressive curve (peak**EXP)
# so quiet speech visibly moves the bars instead of only shouting registering.
# Lower EXP and higher GAIN = more sensitive to soft voices.
_N_BARS = 9
_LEVEL_EXP = 0.4  # <0.5 boosts quiet input more than a plain sqrt would
_LEVEL_GAIN = 3.2  # applied to peak**EXP; normal speech should fill the bars
_BAR_IDLE = 0.10  # baseline height so bars always look alive
_LEVEL_SMOOTHING = 0.55  # 0..1, higher = snappier response to volume
_BAR_SMOOTHING = 0.6
_TICK_SECONDS = 0.033  # ~30fps


class _Action(NSObject):
    """Reusable Obj-C target that forwards ``fire:`` to a Python callable."""

    def initWithCallable_(self, fn):  # noqa: N802 (Obj-C selector name)
        self = objc.super(_Action, self).init()
        if self is None:
            return None
        self._fn = fn
        return self

    def fire_(self, sender):  # noqa: N802
        try:
            self._fn()
        except Exception:
            logger.exception("overlay action failed")


def _rounded_fill(bounds, radius: float) -> None:
    """Fill ``bounds`` with the standard dark translucent background."""
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, radius, radius)
    NSColor.colorWithCalibratedWhite_alpha_(_BG_WHITE, _BG_ALPHA).setFill()
    path.fill()


class _PillView(NSView):
    """Draws the rounded pill plus a live waveform of bars.

    In ``"listening"`` mode the bars react to a mic-level provider (each bar
    also oscillates so the row wiggles). In ``"transcribing"`` mode they run a
    gentle level-independent shimmer in a different tint.
    """

    def initWithFrame_(self, frame):  # noqa: N802
        self = objc.super(_PillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._phase = 0.0
        self._level = 0.0
        self._bars = [_BAR_IDLE] * _N_BARS
        self._mode = "listening"
        self._provider: Callable[[], float] | None = None
        return self

    def configureMode_provider_(self, mode, provider):  # noqa: N802
        self._mode = mode
        self._provider = provider
        self.setNeedsDisplay_(True)

    def tick_(self, timer):  # noqa: N802
        self._phase += 0.22

        target_level = 0.0
        if self._mode == "listening" and self._provider is not None:
            try:
                raw = max(0.0, float(self._provider()))
            except Exception:
                raw = 0.0
            target_level = min(1.0, (raw**_LEVEL_EXP) * _LEVEL_GAIN)
        self._level += (target_level - self._level) * _LEVEL_SMOOTHING

        for i in range(_N_BARS):
            if self._mode == "listening":
                osc = 0.5 + 0.5 * math.sin(self._phase + i * 0.7)
                target = _BAR_IDLE + (0.15 + 0.85 * osc) * self._level
            else:  # transcribing shimmer
                target = 0.22 + 0.18 * (0.5 + 0.5 * math.sin(self._phase * 1.4 + i * 0.9))
            target = max(0.05, min(1.0, target))
            self._bars[i] += (target - self._bars[i]) * _BAR_SMOOTHING

        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):  # noqa: N802
        bounds = self.bounds()
        _rounded_fill(bounds, bounds.size.height / 2.0)

        bar_w, gap = 6.0, 7.0
        total = _N_BARS * bar_w + (_N_BARS - 1) * gap
        x0 = (bounds.size.width - total) / 2.0
        cy = bounds.size.height / 2.0
        max_h = bounds.size.height - 18.0

        color = NSColor.whiteColor() if self._mode == "listening" else NSColor.systemOrangeColor()
        color.colorWithAlphaComponent_(0.92).set()
        for i in range(_N_BARS):
            h = max(3.0, self._bars[i] * max_h)
            bar = NSMakeRect(x0 + i * (bar_w + gap), cy - h / 2.0, bar_w, h)
            radius = bar_w / 2.0
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bar, radius, radius).fill()


class _CardView(NSView):
    """Plain rounded dark background for the result window."""

    def drawRect_(self, rect):  # noqa: N802
        _rounded_fill(self.bounds(), 14.0)


def _make_panel(width: int, height: int, activating_controls: bool) -> NSPanel:
    """Create a borderless, floating, non-activating panel of the given size."""
    rect = NSMakeRect(0, 0, width, height)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect,
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setLevel_(NSScreenSaverWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    panel.setHasShadow_(True)
    panel.setIgnoresMouseEvents_(not activating_controls)
    return panel


def _bottom_center_origin(width: float, bottom_margin: float) -> NSMakePoint:
    """Screen-space origin that centers ``width`` horizontally at ``bottom_margin``."""
    screen = NSScreen.mainScreen()
    if screen is None:
        return NSMakePoint(0, 0)
    frame = screen.frame()
    x = frame.origin.x + (frame.size.width - width) / 2.0
    y = frame.origin.y + bottom_margin
    return NSMakePoint(x, y)


class Overlay:
    """The bottom-center listening pill. All methods are main-thread only.

    ``level_provider`` is an optional callable returning the current mic peak
    (0..1); it is kept decoupled (a plain callable, not the recorder itself) so
    this module never imports a sibling. ``main`` passes ``recorder.level``.
    """

    def __init__(self, level_provider: Callable[[], float] | None = None) -> None:
        self._level_provider = level_provider
        self._panel: NSPanel | None = None
        self._view: _PillView | None = None
        self._timer: NSTimer | None = None

    def _ensure(self) -> None:
        if self._panel is not None:
            return
        w, h = config.INDICATOR_WIDTH, config.INDICATOR_HEIGHT
        panel = _make_panel(w, h, activating_controls=False)
        view = _PillView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        view.setWantsLayer_(True)
        panel.setContentView_(view)
        self._panel, self._view = panel, view

    def _place(self) -> None:
        if self._panel is not None:
            origin = _bottom_center_origin(
                self._panel.frame().size.width, config.INDICATOR_BOTTOM_MARGIN
            )
            self._panel.setFrameOrigin_(origin)

    def _start_timer(self) -> None:
        if self._timer is not None:
            return
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            _TICK_SECONDS, self._view, b"tick:", None, True
        )

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def show_listening(self) -> None:
        self._ensure()
        self._place()
        self._view.configureMode_provider_("listening", self._level_provider)
        self._start_timer()
        self._panel.orderFrontRegardless()

    def show_transcribing(self) -> None:
        self._ensure()
        self._place()
        self._view.configureMode_provider_("transcribing", None)
        self._start_timer()  # keep animating the shimmer
        self._panel.orderFrontRegardless()

    def hide(self) -> None:
        self._stop_timer()
        if self._panel is not None:
            self._panel.orderOut_(None)


class ResultWindow:
    """A small floating card with the transcript and a Copy button.

    Shown only when a paste is blocked (or when the user opts into always
    showing it). Non-activating, so clicking Copy does not pull focus off the
    app you were dictating into. Auto-closes after a linger.
    """

    _WIDTH = 380
    _MARGIN = 18

    def __init__(self) -> None:
        self._panel: NSPanel | None = None
        self._title: NSTextField | None = None
        self._body: NSTextField | None = None
        self._copy_button: NSButton | None = None
        self._text = ""
        self._close_timer: NSTimer | None = None
        # Retain Obj-C action targets so they are not garbage-collected.
        self._copy_action = _Action.alloc().initWithCallable_(self._copy)
        self._close_action = _Action.alloc().initWithCallable_(self.close)

    def _ensure(self) -> None:
        if self._panel is not None:
            return
        w = self._WIDTH
        # Height is finalized in show(); start with a placeholder.
        panel = _make_panel(w, 140, activating_controls=True)
        card = _CardView.alloc().initWithFrame_(NSMakeRect(0, 0, w, 140))
        card.setWantsLayer_(True)
        panel.setContentView_(card)

        title = self._make_label(NSFont.boldSystemFontOfSize_(12.0), NSColor.systemGrayColor())
        body = self._make_label(NSFont.systemFontOfSize_(14.0), NSColor.whiteColor())
        body.setSelectable_(True)
        body.cell().setWraps_(True)

        copy_button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 84, 26))
        copy_button.setTitle_("Copy")
        copy_button.setBezelStyle_(1)  # NSBezelStyleRounded
        copy_button.setTarget_(self._copy_action)
        copy_button.setAction_(b"fire:")

        close_button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 26, 26))
        close_button.setTitle_("✕")
        close_button.setBezelStyle_(1)
        close_button.setTarget_(self._close_action)
        close_button.setAction_(b"fire:")

        for v in (title, body, copy_button, close_button):
            card.addSubview_(v)

        self._panel = panel
        self._card = card
        self._title = title
        self._body = body
        self._copy_button = copy_button
        self._close_button = close_button

    @staticmethod
    def _make_label(font, color) -> NSTextField:
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(font)
        label.setTextColor_(color)
        return label

    def show(self, text: str, reason: str = "") -> None:
        self._ensure()
        self._text = text
        self._cancel_close_timer()

        title = reason or "Dictated text — copy it"
        self._title.setStringValue_(title)
        self._body.setStringValue_(text)
        self._copy_button.setTitle_("Copy")

        w = self._WIDTH
        inner = w - 2 * self._MARGIN
        # Size the body to its wrapped height, then lay the card out around it.
        body_size = self._body.cell().cellSizeForBounds_(NSMakeRect(0, 0, inner, 400))
        body_h = min(max(body_size.height, 20.0), 220.0)
        title_h = 16.0
        button_h = 26.0
        gap = 10.0
        height = self._MARGIN + button_h + gap + body_h + gap + title_h + self._MARGIN

        self._panel.setContentSize_((w, height))
        self._card.setFrame_(NSMakeRect(0, 0, w, height))

        y = height - self._MARGIN - title_h
        self._title.setFrame_(NSMakeRect(self._MARGIN, y, inner, title_h))
        y -= gap + body_h
        self._body.setFrame_(NSMakeRect(self._MARGIN, y, inner, body_h))
        y -= gap + button_h
        self._copy_button.setFrame_(NSMakeRect(w - self._MARGIN - 84, y, 84, button_h))
        self._close_button.setFrame_(NSMakeRect(self._MARGIN, y, 26, button_h))

        origin = _bottom_center_origin(
            w, config.INDICATOR_BOTTOM_MARGIN + config.INDICATOR_HEIGHT + 14
        )
        self._panel.setFrameOrigin_(origin)
        self._panel.orderFrontRegardless()

        if config.RESULT_WINDOW_LINGER_SECONDS > 0:
            self._close_timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    config.RESULT_WINDOW_LINGER_SECONDS, self._close_action, b"fire:", None, False
                )
            )

    def _copy(self) -> None:
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(self._text, NSPasteboardTypeString)
        if self._copy_button is not None:
            self._copy_button.setTitle_("Copied ✓")
        logger.debug("result window: copied %d chars to clipboard", len(self._text))

    def _cancel_close_timer(self) -> None:
        if self._close_timer is not None:
            self._close_timer.invalidate()
            self._close_timer = None

    def close(self) -> None:
        self._cancel_close_timer()
        if self._panel is not None:
            self._panel.orderOut_(None)


if __name__ == "__main__":
    # Standalone manual test: flash the pill through its states, then show the
    # result window. Requires a GUI session.
    from PyObjCTools import AppHelper

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")

    # Synthetic mic level so the bars visibly react without a real recording.
    _clock = {"t": 0.0}

    def _fake_level() -> float:
        _clock["t"] += 0.08
        return 0.12 + 0.09 * (1.0 + math.sin(_clock["t"]))

    overlay = Overlay(level_provider=_fake_level)
    window = ResultWindow()

    def _demo() -> None:
        print("showing listening pill…")
        overlay.show_listening()
        AppHelper.callLater(2.5, lambda: overlay.show_transcribing())
        AppHelper.callLater(4.0, lambda: overlay.hide())
        AppHelper.callLater(
            4.5,
            lambda: window.show(
                "Hello world, this is the voice2text result window — click Copy.",
                "Secure Keyboard Entry is on, so macOS blocked the paste.",
            ),
        )
        AppHelper.callLater(12.0, AppHelper.stopEventLoop)

    AppHelper.callAfter(_demo)
    print("Running for ~12s. Watch the bottom-center of your main display.")
    AppHelper.runEventLoop()
    print("done.")
