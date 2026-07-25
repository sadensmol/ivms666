"""Static frontend assets, loaded from the `static/` directory."""

from pathlib import Path

_STATIC = Path(__file__).parent / "static"

PAGE = (_STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
