"""RTSP URL building and live-view pre-flight checks."""

import io
import unittest

from cameraviewer import live


class _CapturePopen:
    """Captures the ffmpeg argv and returns a fake process emitting one JPEG."""
    last_cmd = None

    def __init__(self, cmd, **kw):
        _CapturePopen.last_cmd = cmd
        self.stdout = io.BytesIO(b"\xff\xd8jpeg\xff\xd9")
        self.stderr = io.BytesIO(b"")

    def communicate(self, timeout=None):
        return self.stdout.read(), b""


class AspectRatioTest(unittest.TestCase):
    def _cmd_for(self, **kw):
        orig = live.subprocess.Popen
        live.subprocess.Popen = _CapturePopen
        try:
            live.grab_still("rtsp://x/1", **kw)
        finally:
            live.subprocess.Popen = orig
        return _CapturePopen.last_cmd

    def test_grab_still_forces_square_pixels(self):
        cmd = self._cmd_for()
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("setsar=1", vf)          # anamorphic (SAR!=1) sources de-squished

    def test_grab_still_thumbnail_scales_after_square_correction(self):
        cmd = self._cmd_for(width=480)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("setsar=1", vf)
        self.assertIn("scale=480:-2", vf)      # width scale applied on top of the correction


class AudioOnlyTest(unittest.TestCase):
    def test_no_video_detects_audio_only(self):
        self.assertTrue(live.no_video(
            "[out#0/mjpeg @ 0x1] Output file does not contain any stream"))
        self.assertTrue(live.no_video("Output file is empty, nothing was encoded"))
        self.assertFalse(live.no_video("Error during demuxing: Operation timed out"))

    def test_open_audio_builds_mp3_command(self):
        orig = live.subprocess.Popen
        live.subprocess.Popen = _CapturePopen
        try:
            live.open_audio("rtsp://x/1")
        finally:
            live.subprocess.Popen = orig
        cmd = _CapturePopen.last_cmd
        self.assertIn("-vn", cmd)                       # drop video (there is none)
        self.assertEqual(cmd[cmd.index("-f") + 1], "mp3")
        self.assertIn("libmp3lame", cmd)               # re-encode G.711 -> browser-playable MP3


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

    def test_url_only_device_uses_stored_url_verbatim(self):
        cfg = {"kind": "rtsp", "rtsp_url": "rtsp://u:p@cam.local:8554/live/0"}
        # channel/stream are ignored — the user's URL is used as-is
        self.assertEqual(live.rtsp_url(cfg, "101", "sub"), "rtsp://u:p@cam.local:8554/live/0")

    def test_host_port_parsed_from_stored_url(self):
        self.assertEqual(
            live._host_port({"kind": "rtsp", "rtsp_url": "rtsp://u:p@cam.local:8554/live"}),
            ("cam.local", 8554))
        # no explicit port -> RTSP default
        self.assertEqual(
            live._host_port({"kind": "rtsp", "rtsp_url": "rtsp://cam.local/live"}),
            ("cam.local", 554))


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
