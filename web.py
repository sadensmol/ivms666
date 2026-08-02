"""Static frontend assets, loaded from the `static/` directory.

Read fresh from disk on each request (not cached at import time) so editing
`index.html`/`app.js` shows up on a plain browser reload — no server restart.
These files are tiny and this is a local single-user tool, so per-request reads
are free.
"""

from pathlib import Path

_STATIC = Path(__file__).parent / "static"


def page():
    return (_STATIC / "index.html").read_text(encoding="utf-8")


def app_js():
    return (_STATIC / "app.js").read_text(encoding="utf-8")


def watch_page():
    """Standalone player for a shared event link (`/watch?device=&ch=&start=&end=`).
    It carries no credentials: the server resolves the device from its own config,
    and Cloudflare Access still gates who may open it at all."""
    return (_STATIC / "watch.html").read_text(encoding="utf-8")
