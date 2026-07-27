"""Recorded playback: grab a single full-resolution still from the DVR's
recording at a chosen time, via RTSP playback + ffmpeg.

The DVR records continuously; ISAPI content-search (`/ISAPI/ContentMgmt/search`)
exposes playback URIs like
`rtsp://<lan-ip>/Streaming/tracks/<track>/?starttime=...&endtime=...`. We build
our own playback URL against the device's EXTERNAL host + forwarded RTSP port
(the LAN ip in the search result is unreachable from outside) and let ffmpeg
decode one frame at that instant — full main-stream resolution (~1080p), which is
sharper than the 720p live snapshot.

Times use the DVR's own wall clock, formatted `YYYYMMDDThhmmssZ` (the device
labels its local timeline with a trailing Z; we do NOT convert time zones).
"""

import subprocess
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from . import live  # reuse ffmpeg_available / rtsp_port

# This OEM DVR caps concurrent RTSP sessions HARD — a second/third simultaneous
# playback grab (or one opened while a stale session lingers) gets
# `RTSP 453 Not Enough Bandwidth`, which is why the event-log thumbnails came back
# broken. So grab frames ONE AT A TIME (semaphore) and retry a 453 after a short
# wait (a slot frees when the previous grab's ffmpeg exits).
_grab_sem = threading.BoundedSemaphore(1)

# A long-lived RTSP stream (clip playback) takes PRIORITY over thumbnail grabs:
# the DVR serves ~one RTSP session, so while a clip plays, grabs stand down (return
# "busy") instead of competing and 453-ing the clip. `_playback_active` counts
# active priority streams; entering rtsp_priority() also drains the in-flight grab
# (via the same semaphore) so the clip never starts against a live grab.
_playback_active = 0
_playback_lock = threading.Lock()


class rtsp_priority:
    """Context manager for a long-lived RTSP stream (clip playback). Claims the
    DVR's single session for its whole duration: new thumbnail grabs stand down,
    and any in-flight grab is drained first. Enter before starting the stream, exit
    when it ends."""

    def __enter__(self):
        global _playback_active
        with _playback_lock:
            _playback_active += 1
        _grab_sem.acquire()   # wait out any in-flight grab, then hold exclusivity
        return self

    def __exit__(self, *exc):
        global _playback_active
        _grab_sem.release()
        with _playback_lock:
            _playback_active -= 1
        return False

# Small in-memory cache of frames already grabbed this session. The frame still
# originates live from the DVR (the source of truth — see CLAUDE.md); this only
# avoids re-fetching the *identical* frame, so a click or a re-open is instant and
# doesn't open another RTSP session. Not persisted to disk, bounded, dropped on
# restart — it is a perf cache, never a data source.
_CACHE_MAX = 400
_cache = {}            # key -> jpeg bytes
_cache_order = []      # insertion order for FIFO eviction
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        return _cache.get(key)


def _cache_put(key, data):
    with _cache_lock:
        if key not in _cache:
            _cache_order.append(key)
            while len(_cache_order) > _CACHE_MAX:
                _cache.pop(_cache_order.pop(0), None)
        _cache[key] = data


def to_span(local_iso, window_secs=6):
    """'YYYY-MM-DDTHH:MM[:SS]' (device wall clock) -> (start, end) as
    'YYYYMMDDThhmmssZ', end = start + window so ffmpeg has a frame to decode."""
    s = local_iso.strip()
    if len(s) == 16:            # datetime-local without seconds
        s += ":00"
    dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    start = dt.strftime("%Y%m%dT%H%M%SZ")
    end = (dt + timedelta(seconds=window_secs)).strftime("%Y%m%dT%H%M%SZ")
    return start, end


def playback_url(cfg, track_id, start, end):
    """Build the Hikvision RTSP playback-by-time URL for one track."""
    user = cfg.get("user") or ""
    pw = cfg.get("password") or ""
    cred = f"{quote(user, safe='')}:{quote(pw, safe='')}@" if user else ""
    return (f"rtsp://{cred}{cfg['host']}:{live.rtsp_port(cfg)}"
            f"/Streaming/tracks/{track_id}/?starttime={start}&endtime={end}")


def _run_ffmpeg(url, timeout, width):
    """One ffmpeg attempt: decode a single JPEG frame. Returns (bytes, stderr)."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url,
           "-frames:v", "1", "-q:v", "4"]
    if width:
        cmd += ["-vf", f"scale={int(width)}:-2"]
    cmd += ["-f", "mjpeg", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return out, (err or b"").decode("utf-8", "replace")


def grab_frame(url, timeout=25, width=None, retries=4):
    """Decode a single JPEG frame from a playback RTSP URL, cached and serialized.
    `width` (px) scales the output down (thumbnails). Returns (jpeg_bytes,
    stderr_text); jpeg_bytes is empty on failure. Repeated calls for the same
    (url, width) return the cached frame instantly. Grabs run one-at-a-time and an
    `RTSP 453 Not Enough Bandwidth` is retried with backoff (the DVR frees a slot
    when the previous grab finishes)."""
    key = (url, width)
    cached = _cache_get(key)
    if cached is not None:
        return cached, ""
    out, err = b"", ""
    for attempt in range(retries):
        if _playback_active:                  # a clip owns the DVR's RTSP session -> yield to it
            return b"", "busy: playback in progress"
        if not _grab_sem.acquire(timeout=20):  # one RTSP session at a time (don't block forever behind a clip)
            return b"", "busy: RTSP session in use"
        try:
            out, err = _run_ffmpeg(url, timeout, width)
        finally:
            _grab_sem.release()
        if out:
            _cache_put(key, out)
            return out, err
        if "Bandwidth" not in err and "453" not in err:
            break                             # a non-bandwidth failure won't fix itself
        time.sleep(0.8 * (attempt + 1))       # wait for the DVR to free a session, then retry
    return out, err
