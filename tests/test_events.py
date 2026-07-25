"""Motion alert-stream monitor: multipart parsing, hold window, save debounce.

No network — we drive Monitor's parser directly with canned <EventNotificationAlert>
bytes and control the clock via events._now.
"""

import unittest
import urllib.error

from cameraviewer import config, events


def alert(ch, etype="VMD", state="active"):
    """One <EventNotificationAlert> element (what _drain hands to _handle)."""
    return (
        '<EventNotificationAlert version="2.0" '
        'xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        f"<channelID>{ch}</channelID><eventType>{etype}</eventType>"
        f"<eventState>{state}</eventState></EventNotificationAlert>"
    ).encode()


def framed(block):
    """Wrap an alert element in the DVR's multipart framing to prove _drain
    skips the boundary/headers around it."""
    return (b"--boundary\r\nContent-Type: application/xml\r\n"
            b"Content-Length: %d\r\n\r\n" % len(block)) + block + b"\r\n"


class FakeResp:
    def __init__(self, data):
        self.data = data
        self.i = 0

    def read(self, n=1024):
        out = self.data[self.i:self.i + n]
        self.i += len(out)
        return out

    def close(self):
        pass


class EventsTest(unittest.TestCase):
    def setUp(self):
        self._orig_now = events._now
        self.clock = [1000.0]
        events._now = lambda: self.clock[0]
        # a device so config.get_cfg / device_label resolve inside _maybe_save
        config._state = {"devices": [{"id": "dev", "name": "Cam", "host": "h", "port": "80"}]}
        self.saves = []
        self.m = events.Monitor("dev", saver=self._saver)

    def tearDown(self):
        events._now = self._orig_now
        config._state = {"devices": []}

    def _saver(self, cfg, ch, label="camera", motion=False):
        self.saves.append((ch, label, motion))

    def _active(self, ch):
        return self.m.state()["channels"].get(str(ch), False)

    # --- tests -------------------------------------------------------------
    def test_active_sets_state_and_saves_once(self):
        self.m._handle(alert(1, "VMD", "active"))
        self.assertTrue(self._active(1))
        self.assertEqual(self.saves, [("101", "Cam", True)])  # input 1 -> picture id 101
        self.m._handle(alert(1, "VMD", "active"))             # still active, no new transition
        self.assertEqual(len(self.saves), 1)

    def test_inactive_clears_state(self):
        self.m._handle(alert(1, "VMD", "active"))
        self.m._handle(alert(1, "VMD", "inactive"))
        self.assertFalse(self._active(1))

    def test_hold_window_expires(self):
        self.m._handle(alert(1, "VMD", "active"))
        self.assertTrue(self._active(1))
        self.clock[0] += events.HOLD_SECONDS + 1
        self.assertFalse(self._active(1))  # no fresh event within the hold window

    def test_save_debounced_across_episodes(self):
        self.m._handle(alert(1, "VMD", "active"))    # saves
        self.m._handle(alert(1, "VMD", "inactive"))
        self.m._handle(alert(1, "VMD", "active"))    # new transition but within debounce
        self.assertEqual(len(self.saves), 1)
        self.clock[0] += events.SAVE_DEBOUNCE + 1
        self.m._handle(alert(1, "VMD", "inactive"))
        self.m._handle(alert(1, "VMD", "active"))    # debounce elapsed -> saves again
        self.assertEqual(len(self.saves), 2)

    def test_non_vmd_events_ignored(self):
        self.m._handle(alert(1, "videoloss", "active"))
        self.assertFalse(self._active(1))
        self.assertEqual(self.saves, [])

    def test_multipart_stream_tracks_all_channels(self):
        self.m._consume(FakeResp(framed(alert(1, "VMD", "active")) +
                                 framed(alert(2, "VMD", "active"))))
        self.assertTrue(self._active(1))
        self.assertTrue(self._active(2))

    def test_split_block_across_reads_is_parsed(self):
        # a block spanning two reads must still be handled once fully buffered
        data = alert(3, "VMD", "active")
        buf = self.m._drain(data[:20])
        self.assertFalse(self._active(3))     # not complete yet
        self.m._drain(buf + data[20:])
        self.assertTrue(self._active(3))

    def test_http_404_marks_unsupported_and_stops(self):
        orig = events.camera.open_stream

        def raise404(cfg, path, timeout=60):
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)

        events.camera.open_stream = raise404
        try:
            self.m._run()  # returns immediately (does not loop/hammer)
        finally:
            events.camera.open_stream = orig
        self.assertFalse(self.m.supported)
        self.assertFalse(self.m.state()["ok"])


if __name__ == "__main__":
    unittest.main()
