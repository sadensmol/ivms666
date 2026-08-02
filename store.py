"""Server-side snapshot saving.

Writes JPEG stills to the user-configured folder (`config` setting `save_path`)
at the device's maximum still resolution (`camera.MAX_STILL_RES`). Used by the
manual Save action and by motion auto-capture. A browser can't write to an
arbitrary path, so saving happens here on the server.

Filenames: `<label>_<channel>_<YYYYmmdd-HHMMSS>[_motion].jpg` (label sanitized).
"""

import os
import re
import time

from . import camera, config


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _safe(name):
    """Reduce a label to filename-safe characters."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-")
    return name or "camera"


def save_bytes(data, channel_id, label="camera", motion=False):
    """Write already-fetched JPEG bytes to the configured folder (created if
    missing). Returns the absolute file path. Used for both ISAPI stills and
    ffmpeg-grabbed RTSP frames."""
    folder = config.get_settings()["save_path"]
    os.makedirs(folder, exist_ok=True)
    suffix = "_motion" if motion else ""
    # channel_id is caller/browser-supplied — sanitize it (like the label) so it
    # can't smuggle path separators into the filename, then confirm the resolved
    # path stays inside the save folder before writing.
    fname = f"{_safe(label)}_{_safe(str(channel_id))}_{_stamp()}{suffix}.jpg"
    path = os.path.join(folder, fname)
    if os.path.commonpath([os.path.realpath(path), os.path.realpath(folder)]) != os.path.realpath(folder):
        raise ValueError("refusing to write outside the save folder")
    with open(path, "wb") as f:
        f.write(data)
    return path


def save_snapshot(cfg, channel_id, label="camera", motion=False):
    """Fetch a max-resolution JPEG for a channel and write it to the configured
    folder. Returns the absolute file path. Raises on a fetch or write error so
    the caller can surface it."""
    _, data = camera.fetch_snapshot(cfg, channel_id, camera.MAX_STILL_RES)
    return save_bytes(data, channel_id, label, motion)
