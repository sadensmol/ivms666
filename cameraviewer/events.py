"""Motion-event monitoring via the Hikvision ISAPI alert stream.

Each configured device gets a background daemon thread holding a long-lived GET
on `/ISAPI/Event/notification/alertStream`. The DVR pushes a `multipart/mixed`
stream of `<EventNotificationAlert>` XML chunks; we watch the video-motion (VMD)
ones and track per-channel motion state.

A channel is reported "active" when a VMD `active` event was seen within
HOLD_SECONDS — this covers firmwares that repeat `active` while motion lasts and
then simply stop (no explicit `inactive`). On a fresh inactive->active
transition we save a snapshot (debounced per channel).

Stdlib only. The stream reconnects with backoff on drop; a 404/501/403 marks the
device "unsupported" and stops retrying (some DVRs don't expose the stream, and
hammering a 403 risks the config-write lockout documented in CLAUDE.md).

The `channelID` in a VMD event is the physical video-input index (1..4), which
is exactly the `input` the frontend already keys its tiles by; the picture id
for the auto-save is derived as `<input>01`.
"""

import threading
import time
import urllib.error

from . import camera, config, store

ALERT_PATH = "/ISAPI/Event/notification/alertStream"
HOLD_SECONDS = 6.0      # a channel stays "active" this long after its last active event
SAVE_DEBOUNCE = 10.0    # min seconds between motion auto-saves for one channel
STREAM_TIMEOUT = 60     # per-read socket timeout; a silent stream past it reconnects
BACKOFF_SECONDS = 10.0  # wait before reconnecting after a stream error
_TAG_CLOSE = b"</EventNotificationAlert>"
_TAG_OPEN = b"<EventNotificationAlert"


def _now():
    """Indirection so tests can control the clock (patch events._now)."""
    return time.monotonic()


def _localname(tag):
    return tag.rsplit("}", 1)[-1]


def _text(root, localname):
    for node in root.iter():
        if _localname(node.tag) == localname:
            return (node.text or "").strip()
    return None


def _picture_id(input_id):
    """Video-input index (e.g. "1") -> picture/stream id ("101")."""
    try:
        return f"{int(input_id)}01"
    except ValueError:
        return input_id


class Monitor:
    """Holds one device's alert-stream connection and its motion state."""

    def __init__(self, device_id, saver=None):
        self.device_id = device_id
        self.saver = saver or store.save_snapshot
        self._lock = threading.Lock()
        self._active = {}      # input(str) -> monotonic ts of last active event
        self._last_save = {}   # input(str) -> monotonic ts of last auto-save
        self.supported = True
        self.message = "starting"
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def state(self):
        """Browser-facing snapshot: {ok, supported, message, channels:{input:bool}}."""
        now = _now()
        with self._lock:
            channels = {ch: (now - ts) < HOLD_SECONDS for ch, ts in self._active.items()}
        return {"ok": self.supported, "supported": self.supported,
                "message": self.message, "channels": channels}

    # --- internals ---------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            cfg = config.get_cfg(self.device_id)
            if not cfg:            # device deleted -> retire this monitor
                return
            try:
                resp = camera.open_stream(cfg, ALERT_PATH, timeout=STREAM_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code in (404, 501, 403):
                    self.supported = False
                    self.message = f"alert stream unavailable (HTTP {e.code})"
                    return         # don't hammer an endpoint the DVR won't serve
                self.message = f"HTTP {e.code}"
                self._stop.wait(BACKOFF_SECONDS)
                continue
            except Exception as e:  # noqa: BLE001 - connection/timeout -> retry
                self.message = f"{type(e).__name__}: {e}"
                self._stop.wait(BACKOFF_SECONDS)
                continue
            self.message = "connected"
            try:
                self._consume(resp)
            except Exception as e:  # noqa: BLE001 - stream dropped -> reconnect
                self.message = f"stream ended: {type(e).__name__}"
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            self._stop.wait(BACKOFF_SECONDS)

    def _consume(self, resp):
        """Read the multipart stream and dispatch each complete alert block."""
        buf = b""
        while not self._stop.is_set():
            chunk = resp.read(1024)
            if not chunk:
                break
            buf = self._drain(buf + chunk)
            if len(buf) > 65536:   # guard against unbounded growth on junk
                buf = buf[-4096:]

    def _drain(self, buf):
        """Extract every complete <EventNotificationAlert>...</> block, act on it,
        and return the unconsumed tail."""
        while True:
            end = buf.find(_TAG_CLOSE)
            if end == -1:
                return buf
            end += len(_TAG_CLOSE)
            start = buf.rfind(_TAG_OPEN, 0, end)
            if start != -1:
                self._handle(buf[start:end])
            buf = buf[end:]

    def _handle(self, xml_bytes):
        try:
            root = camera.parse_xml(xml_bytes)
        except Exception:
            return
        if (_text(root, "eventType") or "").lower() != "vmd":
            return
        ch = (_text(root, "channelID") or _text(root, "dynChannelID") or "").strip()
        if not ch:
            return
        state = (_text(root, "eventState") or "").lower()
        now = _now()
        transition = False
        with self._lock:
            was_active = (now - self._active.get(ch, 0.0)) < HOLD_SECONDS
            if state == "inactive":
                self._active[ch] = 0.0
            else:  # active (or unlabeled -> treat as active)
                self._active[ch] = now
                transition = not was_active
        if transition:
            self._maybe_save(ch, now)

    def _maybe_save(self, ch, now):
        with self._lock:
            if now - self._last_save.get(ch, 0.0) < SAVE_DEBOUNCE:
                return
            self._last_save[ch] = now
        cfg = config.get_cfg(self.device_id)
        if not cfg:
            return
        try:
            self.saver(cfg, _picture_id(ch), label=config.device_label(self.device_id),
                       motion=True)
        except Exception:
            pass  # a failed save must never kill the monitor


# --- module-level manager ---------------------------------------------------
_monitors = {}
_mlock = threading.Lock()


def ensure(device_id):
    """Return the device's monitor, starting it on first use."""
    with _mlock:
        m = _monitors.get(device_id)
        if m is None:
            m = Monitor(device_id)
            _monitors[device_id] = m
            m.start()
        return m


def get_state(device_id):
    return ensure(device_id).state()


def start_all():
    """Start a monitor for every configured device (called at server startup so
    motion capture works even with no browser open)."""
    for d in config.list_devices():
        ensure(d["id"])


def stop(device_id):
    with _mlock:
        m = _monitors.pop(device_id, None)
    if m:
        m.stop()


def stop_all():
    with _mlock:
        monitors = list(_monitors.values())
        _monitors.clear()
    for m in monitors:
        m.stop()
