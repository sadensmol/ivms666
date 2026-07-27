"""Recorded-playback URL/time helpers + grab_frame cache/serialize/retry logic
(ffmpeg itself is exercised end-to-end; here we fake _run_ffmpeg)."""

import threading
import time
import unittest
from unittest import mock

from cameraviewer import playback

JPEG = b"\xff\xd8jpeg\xff\xd9"
BW_ERR = "[rtsp @ 0x1] method DESCRIBE failed: 453Not Enough Bandwidth"


class PlaybackTest(unittest.TestCase):
    def test_to_span_with_seconds(self):
        start, end = playback.to_span("2026-07-25T14:00:00")
        self.assertEqual(start, "20260725T140000Z")
        self.assertEqual(end, "20260725T140006Z")  # default 6s window

    def test_to_span_without_seconds(self):
        start, _ = playback.to_span("2026-07-25T14:00")  # datetime-local, no seconds
        self.assertEqual(start, "20260725T140000Z")

    def test_to_span_rejects_garbage(self):
        with self.assertRaises(ValueError):
            playback.to_span("not-a-time")

    def test_playback_url_encodes_credentials_and_time(self):
        cfg = {"host": "h", "port": "80", "user": "admin", "password": "p@ss", "rtsp_port": "8554"}
        u = playback.playback_url(cfg, "101", "20260725T140000Z", "20260725T140006Z")
        self.assertIn("rtsp://admin:p%40ss@h:8554/Streaming/tracks/101/", u)
        self.assertIn("starttime=20260725T140000Z", u)
        self.assertIn("endtime=20260725T140006Z", u)

    def test_playback_url_default_rtsp_port(self):
        u = playback.playback_url({"host": "h", "port": "80"}, "201", "a", "b")
        self.assertIn("rtsp://h:554/Streaming/tracks/201/", u)  # no creds, default port


class GrabFrameTest(unittest.TestCase):
    def setUp(self):
        # isolate each test from the module-level frame cache
        playback._cache.clear()
        playback._cache_order.clear()

    def test_caches_success_so_second_grab_skips_ffmpeg(self):
        calls = []
        with mock.patch.object(playback, "_run_ffmpeg", lambda *a: (calls.append(1) or (JPEG, ""))):
            a, _ = playback.grab_frame("rtsp://x/1", width=480)
            b, _ = playback.grab_frame("rtsp://x/1", width=480)
        self.assertEqual(a, JPEG)
        self.assertEqual(b, JPEG)
        self.assertEqual(len(calls), 1)  # second call served from cache

    def test_different_width_is_a_distinct_cache_entry(self):
        calls = []
        with mock.patch.object(playback, "_run_ffmpeg", lambda *a: (calls.append(1) or (JPEG, ""))):
            playback.grab_frame("rtsp://x/2", width=480)
            playback.grab_frame("rtsp://x/2", width=None)
        self.assertEqual(len(calls), 2)

    def test_retries_on_453_then_succeeds(self):
        seq = [(b"", BW_ERR), (JPEG, "")]
        with mock.patch.object(playback, "_run_ffmpeg", lambda *a: seq.pop(0)), \
                mock.patch.object(playback.time, "sleep", lambda *_: None):
            data, _ = playback.grab_frame("rtsp://x/3")
        self.assertEqual(data, JPEG)
        self.assertEqual(seq, [])  # both attempts consumed

    def test_gives_up_immediately_on_non_bandwidth_error(self):
        calls = []
        with mock.patch.object(playback, "_run_ffmpeg",
                               lambda *a: (calls.append(1) or (b"", "connection refused"))), \
                mock.patch.object(playback.time, "sleep", lambda *_: None):
            data, err = playback.grab_frame("rtsp://x/4")
        self.assertEqual(data, b"")
        self.assertEqual(len(calls), 1)  # not retried — a non-453 error won't self-heal

    def test_grabs_stand_down_under_playback_priority_then_resume(self):
        calls = []
        with mock.patch.object(playback, "_run_ffmpeg",
                               lambda *a: (calls.append(1) or (JPEG, ""))):
            with playback.rtsp_priority():                 # a clip owns the RTSP session
                data, err = playback.grab_frame("rtsp://x/p1")
                self.assertEqual(data, b"")                # grab yielded, ffmpeg never ran
                self.assertIn("playback", err)
            after, _ = playback.grab_frame("rtsp://x/p2")  # priority released -> grabs work again
            self.assertEqual(after, JPEG)
        self.assertEqual(len(calls), 1)                    # only the post-release grab ran

    def test_grabs_run_one_at_a_time(self):
        concurrent = []
        peak = [0]
        lock = threading.Lock()

        def fake(url, timeout, width):
            with lock:
                concurrent.append(1)
                peak[0] = max(peak[0], len(concurrent))
            time.sleep(0.05)
            with lock:
                concurrent.pop()
            return (url.encode(), "")  # distinct bytes per url -> no cache collisions

        with mock.patch.object(playback, "_run_ffmpeg", fake):
            threads = [threading.Thread(target=playback.grab_frame, args=(f"rtsp://x/n{i}",))
                       for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak[0], 1)  # the semaphore never let two grabs overlap


if __name__ == "__main__":
    unittest.main()
