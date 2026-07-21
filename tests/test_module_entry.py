from __future__ import annotations

import os
import sys
from io import StringIO

from voice2text.gui_streams import attach_missing_gui_streams


def test_gui_stream_fallback_uses_null_device_only_when_stream_is_missing(
    monkeypatch,
) -> None:
    source = StringIO()
    errors = StringIO()
    monkeypatch.setattr(sys, "stdout", source)
    monkeypatch.setattr(sys, "stderr", errors)

    attached = attach_missing_gui_streams()

    assert attached == ()
    assert sys.stdout is source
    assert sys.stderr is errors


def test_gui_stream_fallback_attaches_missing_streams_to_null_device(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    attached = attach_missing_gui_streams()
    try:
        assert len(attached) == 2
        assert sys.stdout is attached[0]
        assert sys.stderr is attached[1]
        assert attached[0].name == os.devnull
        assert attached[1].name == os.devnull
    finally:
        for stream in attached:
            stream.close()
