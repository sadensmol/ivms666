"""RTSP URL building, live-view pre-flight checks and ffmpeg child bookkeeping."""

import io
import unittest

import live


class _CapturePopen:
    """Captures the ffmpeg argv and returns a fake process emitting one JPEG."""
    last_cmd = None

    def __init__(self, cmd, **kw):
        _CapturePopen.last_cmd = cmd
        self.stdout = io.BytesIO(b"\xff\xd8jpeg\xff\xd9")
        self.stderr = io.BytesIO(b"")
        self.killed = False

    def communicate(self, timeout=None):
        return self.stdout.read(), b""

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


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


class ChildProcessTest(unittest.TestCase):
    """Every ffmpeg we spawn must be killable on shutdown: one left behind keeps
    its RTSP session open and the DVR then answers 453 to every later grab."""

    def setUp(self):
        live._procs.clear()

    def _spawn(self):
        orig = live.subprocess.Popen
        live.subprocess.Popen = _CapturePopen
        try:
            return live.spawn(["ffmpeg", "-i", "rtsp://x/1"])
        finally:
            live.subprocess.Popen = orig

    def test_spawn_registers_the_child(self):
        proc = self._spawn()
        self.assertIn(proc, live._procs)

    def test_terminate_kills_and_forgets(self):
        proc = self._spawn()
        live.terminate(proc)
        self.assertTrue(proc.killed)
        self.assertNotIn(proc, live._procs)

    def test_terminate_all_kills_every_child(self):
        procs = [self._spawn(), self._spawn()]
        live.terminate_all()
        self.assertTrue(all(p.killed for p in procs))
        self.assertFalse(live._procs)

    def test_orphan_pids_finds_only_our_abandoned_streams(self):
        ps = "\n".join([
            "  111     1 ffmpeg -i rtsp://u:p@1.2.3.4:8556/Streaming/tracks/101/?starttime=x",
            "  222   999 ffmpeg -i rtsp://u:p@1.2.3.4:8556/Streaming/Channels/101",  # still ours, has a parent
            "  333     1 ffmpeg -i rtsp://9.9.9.9/live",       # orphan, but not our device
            "  444     1 /usr/bin/qemu -display none",         # not ffmpeg
        ])
        self.assertEqual(live._orphan_pids(ps, ["1.2.3.4", "5.6.7.8"]), [111])

    def test_orphan_pids_without_hosts_matches_nothing(self):
        ps = "  111     1 ffmpeg -i rtsp://1.2.3.4/live"
        self.assertEqual(live._orphan_pids(ps, []), [])


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
