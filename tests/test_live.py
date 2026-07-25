"""RTSP URL building and live-view pre-flight checks."""

import unittest

from cameraviewer import live


class RtspUrlTest(unittest.TestCase):
    def test_main_stream_url(self):
        cfg = {"host": "1.2.3.4", "rtsp_port": "554", "user": "admin", "password": "p@ss#1"}
        self.assertEqual(
            live.rtsp_url(cfg, "101", "main"),
            "rtsp://admin:p%40ss%231@1.2.3.4:554/Streaming/Channels/101",
        )

    def test_sub_stream_id(self):
        cfg = {"host": "h", "rtsp_port": "10554", "user": "u", "password": "p"}
        # main 201 -> sub 202, on the custom RTSP port
        self.assertEqual(
            live.rtsp_url(cfg, "201", "sub"),
            "rtsp://u:p@h:10554/Streaming/Channels/202",
        )

    def test_no_credentials(self):
        cfg = {"host": "h", "user": "", "password": ""}
        self.assertEqual(live.rtsp_url(cfg, "101"), "rtsp://h:554/Streaming/Channels/101")

    def test_default_port(self):
        cfg = {"host": "h", "user": "u", "password": "p"}
        self.assertIn(":554/", live.rtsp_url(cfg, "101"))


class CheckTest(unittest.TestCase):
    def test_reports_missing_ffmpeg(self):
        orig = live.ffmpeg_available
        live.ffmpeg_available = lambda: False
        try:
            ok, msg = live.check({"host": "h", "rtsp_port": "554"})
            self.assertFalse(ok)
            self.assertIn("ffmpeg", msg)
        finally:
            live.ffmpeg_available = orig

    def test_reports_unreachable_port(self):
        orig = live.ffmpeg_available
        live.ffmpeg_available = lambda: True
        try:
            # 127.0.0.1 on an unlikely port -> connection refused/timeout
            ok, msg = live.check({"host": "127.0.0.1", "rtsp_port": "9"})
            self.assertFalse(ok)
            self.assertIn("RTSP", msg)
        finally:
            live.ffmpeg_available = orig


if __name__ == "__main__":
    unittest.main()
