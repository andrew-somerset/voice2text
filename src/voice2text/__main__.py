from __future__ import annotations

from voice2text.gui_streams import attach_missing_gui_streams

_GUI_STREAMS = attach_missing_gui_streams()

from voice2text.main import main  # noqa: E402

raise SystemExit(main())
