"""End-to-end HTTP tests through the real request handler (camera layer faked)."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import camera, config, events, live, playback, recordings, server
from tests.helpers import (
    FakeCamera, FakeProc, FAKE_JPEG, OK_RESP, all_on_gridmap, cmsearch_result_xml,
    motion_xml, record_track_xml, stream_caps_xml, stream_channel_xml, time_xml,
    video_inputs_xml)


def _raise_404(path):
    raise urllib.error.HTTPError(path, 404, "Not Found", None, None)


def camera_handler(method, path, body):
    if path == "/ISAPI/System/Video/inputs/channels":
        return ("application/xml", video_inputs_xml(2))
    if "/picture" in path:
        return ("image/jpeg", FAKE_JPEG)
    if path.endswith("/motionDetection"):
        if method == "GET":
            return ("application/xml", motion_xml(all_on_gridmap(), sensitivity=50))
        return ("application/xml", OK_RESP)
    if path == "/ISAPI/System/reboot":
        return ("application/xml", OK_RESP)
    if path == "/ISAPI/System/time":
        return ("application/xml", OK_RESP if method == "PUT" else time_xml(0))  # clock in sync
    if path.startswith("/ISAPI/Event/triggers/VMD-"):
        if method == "PUT":
            return ("application/xml", OK_RESP)
        return ("application/xml",
                b'<EventTrigger xmlns="http://www.std-cgi.com/ver20/XMLSchema">'
                b"<EventTriggerNotificationList>"
                b"<EventTriggerNotification><notificationMethod>record</notificationMethod></EventTriggerNotification>"
                b"<EventTriggerNotification><notificationMethod>email</notificationMethod></EventTriggerNotification>"
                b"</EventTriggerNotificationList></EventTrigger>")  # missing center -> fixable
    if path.startswith("/ISAPI/System/Network/mailing"):
        return ("application/xml",
                b"<mailing><hostName>smtp.x.com</hostName><receiverList>"
                b"<receiver><receiverAddress>a@x.com</receiverAddress></receiver>"
                b"</receiverList></mailing>")
    if path.startswith("/ISAPI/ContentMgmt/record/tracks/"):
        if method == "PUT":
            return ("application/xml", OK_RESP)
        return ("application/xml", record_track_xml())  # CMR/5/5 -> diagnose flags it
    if path.startswith("/ISAPI/Streaming/channels/"):
        if path.endswith("/capabilities"):
            return ("application/xml", stream_caps_xml())
        if method == "PUT":
            return ("application/xml", OK_RESP)
        return ("application/xml", stream_channel_xml())  # 1920x1080 -> quality OK
    if path == "/ISAPI/ContentMgmt/search":
        return ("application/xml",
                cmsearch_result_xml([("2026-07-26T08:01:34Z", "2026-07-26T08:01:51Z")]))
    raise AssertionError("unexpected camera path: " + path)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = config.CONFIG_PATH
        self._orig_legacy = config.LEGACY_CONFIG_PATH
        config.CONFIG_PATH = os.path.join(self.tmp, "cfg.json")
        # keep the pre-rename migration away from the real ~/.camera_viewer.json
        config.LEGACY_CONFIG_PATH = os.path.join(self.tmp, "legacy.json")
        self.save_dir = os.path.join(self.tmp, "shots")
        config._state = {"devices": [], "settings": {"save_path": self.save_dir}}

        self.fake = FakeCamera(camera_handler)
        self.fake.__enter__()
        # keep motion monitors off the real network: any that start exit at once
        self._orig_open_stream = camera.open_stream
        camera.open_stream = lambda cfg, path, timeout=60: _raise_404(path)
        events.stop_all()

        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        events.stop_all()
        camera.open_stream = self._orig_open_stream
        self.fake.__exit__()
        config.CONFIG_PATH = self._orig_path
        config.LEGACY_CONFIG_PATH = self._orig_legacy
        config._state = {"devices": []}

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    def req_headers(self, path):
        """(status, headers) — for asserting on Content-Disposition etc."""
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                resp.read()
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            try:
                return e.code, dict(e.headers)
            finally:
                e.close()

    def add_device(self):
        st, b = self.req("POST", "/devices",
                         {"name": "T", "host": "1.1.1.1", "port": "8001",
                          "user": "admin", "password": "secret"})
        return st, json.loads(b)

    # --- tests -------------------------------------------------------------
    def test_index_and_appjs_served(self):
        st, b = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"ivms666", b)
        st, b = self.req("GET", "/app.js")
        self.assertEqual(st, 200)
        self.assertIn(b"loadDevices", b)

    def test_add_list_device_never_leaks_password(self):
        st, dev = self.add_device()
        self.assertEqual(st, 200)
        self.assertTrue(dev["hasPassword"])
        st, b = self.req("GET", "/devices")
        self.assertEqual(st, 200)
        self.assertNotIn(b"secret", b)  # password must not reach the browser
        self.assertEqual(len(json.loads(b)), 1)

    def test_add_requires_host_and_port(self):
        st, _ = self.req("POST", "/devices", {"name": "x"})
        self.assertEqual(st, 400)

    # --- URL-only RTSP stream devices --------------------------------------
    def add_rtsp(self):
        st, b = self.req("POST", "/devices",
                         {"name": "Yard", "rtsp_url": "rtsp://u:sesame@cam:8554/live"})
        return st, json.loads(b)

    def test_add_rtsp_device_masks_url_and_sets_kind(self):
        st, dev = self.add_rtsp()
        self.assertEqual(st, 200)
        self.assertEqual(dev["kind"], "rtsp")
        self.assertNotIn("sesame", json.dumps(dev))            # password in URL never returned
        _, listed = self.req("GET", "/devices")
        self.assertNotIn(b"sesame", listed)

    def test_rtsp_channels_are_a_single_synthetic_channel(self):
        _, dev = self.add_rtsp()
        st, b = self.req("GET", "/channels?device=" + dev["id"])
        self.assertEqual(st, 200)
        chans = json.loads(b)
        self.assertEqual([c["id"] for c in chans], ["rtsp"])

    def test_rtsp_snapshot_grabs_a_frame_from_the_stream(self):
        _, dev = self.add_rtsp()
        orig_a, orig_g = live.ffmpeg_available, live.grab_still
        live.ffmpeg_available = lambda: True
        live.grab_still = lambda url, width=None: (FAKE_JPEG, "")
        try:
            st, b = self.req("GET", "/snapshot?device=" + dev["id"] + "&ch=rtsp&res=640x360")
            self.assertEqual(st, 200)
            self.assertEqual(b, FAKE_JPEG)
        finally:
            live.ffmpeg_available, live.grab_still = orig_a, orig_g

    def test_rtsp_snapshot_signals_audio_only_when_no_video(self):
        _, dev = self.add_rtsp()
        orig_a, orig_g = live.ffmpeg_available, live.grab_still
        live.ffmpeg_available = lambda: True
        # ffmpeg found no video track -> audio/metadata-only stream
        live.grab_still = lambda url, width=None: (b"", "Output file does not contain any stream")
        try:
            st, b = self.req("GET", "/snapshot?device=" + dev["id"] + "&ch=rtsp&res=640x360")
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(b)["audio_only"])
        finally:
            live.ffmpeg_available, live.grab_still = orig_a, orig_g

    def test_audio_stream_pipes_mp3(self):
        _, dev = self.add_rtsp()
        orig_avail, orig_open = live.ffmpeg_available, live.open_audio
        live.ffmpeg_available = lambda: True
        live.open_audio = lambda url: FakeProc(b"ID3MP3DATA")
        try:
            st, b = self.req("GET", f"/audio?device={dev['id']}&ch=rtsp")
            self.assertEqual(st, 200)
            self.assertIn(b"MP3DATA", b)
        finally:
            live.ffmpeg_available, live.open_audio = orig_avail, orig_open

    def test_rtsp_motion_state_reports_unsupported(self):
        _, dev = self.add_rtsp()
        st, b = self.req("GET", "/motion/state?device=" + dev["id"])
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(b)["supported"])

    def test_rtsp_reason_messages(self):
        self.assertIn("no video", server._rtsp_reason("Error during demuxing: Operation timed out"))
        self.assertIn("authentication", server._rtsp_reason("method DESCRIBE failed: 401 Unauthorized"))
        self.assertIn("path not found", server._rtsp_reason("failed: 404 Not Found"))
        self.assertIn("cannot connect", server._rtsp_reason("Connection refused"))
        # audio/metadata-only stream (no m=video) -> ffmpeg "does not contain any stream"
        self.assertIn("no video track", server._rtsp_reason(
            "[out#0/mjpeg @ 0x1] Output file does not contain any stream"))

    def test_channels_discovery(self):
        _, dev = self.add_device()
        st, b = self.req("GET", f"/channels?device={dev['id']}")
        self.assertEqual(st, 200)
        self.assertEqual([c["id"] for c in json.loads(b)], ["101", "201"])

    def test_snapshot_passes_resolution(self):
        _, dev = self.add_device()
        st, b = self.req("GET", f"/snapshot?device={dev['id']}&ch=101&res=1280x720")
        self.assertEqual(st, 200)
        self.assertEqual(b, FAKE_JPEG)
        self.assertTrue(any("videoResolutionWidth=1280" in p for p in self.fake.gets))

    def test_unknown_device_404(self):
        st, _ = self.req("GET", "/snapshot?device=nope&ch=101")
        self.assertEqual(st, 404)

    def test_motion_get_and_save(self):
        _, dev = self.add_device()
        st, b = self.req("GET", f"/motion?device={dev['id']}&input=1")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(b)["format"], "grid")

        cells = [[1 if c < 11 else 0 for c in range(22)] for _ in range(18)]
        st, b = self.req("POST", "/motion",
                         {"device": dev["id"], "input": "1", "cells": cells, "sensitivity": 80})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(b)["ok"])
        # a PUT actually reached the (fake) camera
        self.assertTrue(any(p.endswith("/motionDetection") for p, _ in self.fake.puts))

    def test_settings_get_and_update(self):
        st, b = self.req("GET", "/settings")
        self.assertEqual(st, 200)
        self.assertIn("save_path", json.loads(b))
        self.assertIs(json.loads(b)["motion_popup"], False)  # default off
        newp = os.path.join(self.tmp, "newshots")
        st, b = self.req("PUT", "/settings", {"save_path": newp})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(b)["save_path"], os.path.abspath(newp))

    def test_motion_popup_setting_round_trips(self):
        st, b = self.req("PUT", "/settings", {"motion_popup": True})
        self.assertEqual(st, 200)
        self.assertIs(json.loads(b)["motion_popup"], True)
        _, b2 = self.req("GET", "/settings")
        self.assertIs(json.loads(b2)["motion_popup"], True)

    def test_save_writes_max_res_jpeg(self):
        _, dev = self.add_device()
        st, b = self.req("POST", "/save", {"device": dev["id"], "ch": "101"})
        self.assertEqual(st, 200)
        data = json.loads(b)
        self.assertTrue(data["ok"])
        self.assertTrue(os.path.exists(data["path"]))          # written under save_dir
        self.assertTrue(data["path"].startswith(self.save_dir))
        self.assertTrue(any("videoResolutionWidth=1280" in p for p in self.fake.gets))
        # unknown device -> ok False, no crash
        _, b2 = self.req("POST", "/save", {"device": "nope", "ch": "101"})
        self.assertFalse(json.loads(b2)["ok"])

    def test_diagnose_route(self):
        _, dev = self.add_device()
        st, b = self.req("GET", f"/diagnose?device={dev['id']}")
        self.assertEqual(st, 200)
        rep = json.loads(b)
        self.assertIn("channels", rep)
        self.assertTrue(rep["smtp"]["ok"])
        self.assertTrue(rep["fixable"])  # fake triggers miss 'center'
        st2, _ = self.req("GET", "/diagnose?device=nope")
        self.assertEqual(st2, 404)

    def test_diagnose_fix_route(self):
        _, dev = self.add_device()
        st, b = self.req("POST", "/diagnose/fix", {"device": dev["id"]})
        self.assertEqual(st, 200)
        r = json.loads(b)
        self.assertTrue(r["ok"])
        # a linkage-adding PUT reached a VMD trigger
        self.assertTrue(any(p.startswith("/ISAPI/Event/triggers/VMD-") for p, _ in self.fake.puts))
        _, b2 = self.req("POST", "/diagnose/fix", {"device": "nope"})
        self.assertFalse(json.loads(b2)["ok"])

    def test_reboot_device(self):
        _, dev = self.add_device()
        st, b = self.req("POST", "/reboot", {"device": dev["id"]})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(b)["ok"])
        self.assertTrue(any(p == "/ISAPI/System/reboot" for p, _ in self.fake.puts))
        _, b2 = self.req("POST", "/reboot", {"device": "nope"})
        self.assertFalse(json.loads(b2)["ok"])

    def test_motion_state_route(self):
        _, dev = self.add_device()
        st, b = self.req("GET", f"/motion/state?device={dev['id']}")
        self.assertEqual(st, 200)
        data = json.loads(b)
        self.assertIn("channels", data)
        self.assertIsInstance(data["channels"], dict)
        st2, _ = self.req("GET", "/motion/state?device=nope")
        self.assertEqual(st2, 404)

    def test_hidden_setup_persists(self):
        _, dev = self.add_device()
        st, _ = self.req("PUT", f"/devices/{dev['id']}", {"hidden": ["201"]})
        self.assertEqual(st, 200)
        with open(config.CONFIG_PATH) as f:
            saved = json.load(f)
        self.assertEqual(saved["devices"][0]["hidden"], ["201"])

    def test_delete_device(self):
        _, dev = self.add_device()
        st, _ = self.req("DELETE", f"/devices/{dev['id']}")
        self.assertEqual(st, 200)
        st, b = self.req("GET", "/devices")
        self.assertEqual(json.loads(b), [])

    # --- live / RTSP -------------------------------------------------------
    def test_rtsp_port_persists(self):
        st, b = self.req("POST", "/devices",
                         {"host": "1.1.1.1", "port": "8001", "rtsp_port": "8554"})
        self.assertEqual(json.loads(b)["rtspPort"], "8554")
        dev = json.loads(b)
        self.req("PUT", f"/devices/{dev['id']}", {"rtsp_port": "9554"})
        st, b = self.req("GET", "/devices")
        self.assertEqual(json.loads(b)[0]["rtspPort"], "9554")

    def test_live_check_reports_result(self):
        _, dev = self.add_device()
        orig = live.check
        live.check = lambda cfg: (False, "cannot reach RTSP")
        try:
            st, b = self.req("GET", f"/live/check?device={dev['id']}")
            self.assertEqual(st, 200)
            data = json.loads(b)
            self.assertFalse(data["ok"])
            self.assertIn("RTSP", data["message"])
        finally:
            live.check = orig

    def test_live_stream_pipes_ffmpeg_output(self):
        _, dev = self.add_device()
        orig_avail, orig_open = live.ffmpeg_available, live.open_mjpeg
        live.ffmpeg_available = lambda: True
        live.open_mjpeg = lambda url: FakeProc(b"--ffmpeg\r\nJPEGDATA\r\n")
        try:
            st, b = self.req("GET", f"/live?device={dev['id']}&ch=101&stream=main")
            self.assertEqual(st, 200)
            self.assertIn(b"JPEGDATA", b)
            # Must be HTTP/1.1 chunked, not an HTTP/1.0 close-delimited body:
            # cloudflared 502s the latter (works on localhost, breaks behind Cloudflare).
            _, hdrs = self.req_headers(f"/live?device={dev['id']}&ch=101&stream=main")
            self.assertEqual(hdrs.get("Transfer-Encoding"), "chunked")
        finally:
            live.ffmpeg_available, live.open_mjpeg = orig_avail, orig_open

    def test_live_stream_reports_failure(self):
        _, dev = self.add_device()
        orig_avail, orig_open = live.ffmpeg_available, live.open_mjpeg
        live.ffmpeg_available = lambda: True
        live.open_mjpeg = lambda url: FakeProc(b"", err=b"Connection refused")  # no frames
        try:
            st, b = self.req("GET", f"/live?device={dev['id']}&ch=101")
            self.assertEqual(st, 502)
            self.assertIn(b"cannot connect", b)  # ffmpeg "refused" -> friendly reason
        finally:
            live.ffmpeg_available, live.open_mjpeg = orig_avail, orig_open

    def test_playback_returns_recorded_frame(self):
        _, dev = self.add_device()
        orig_av, orig_grab = live.ffmpeg_available, playback.grab_frame
        live.ffmpeg_available = lambda: True
        playback.grab_frame = lambda url, timeout=25, width=None: (FAKE_JPEG, "")
        try:
            st, b = self.req("GET", f"/playback?device={dev['id']}&ch=101&time=2026-07-25T14:00:00")
            self.assertEqual(st, 200)
            self.assertEqual(b, FAKE_JPEG)
        finally:
            live.ffmpeg_available, playback.grab_frame = orig_av, orig_grab

    def test_playback_failure_reports_502(self):
        _, dev = self.add_device()
        orig_av, orig_grab = live.ffmpeg_available, playback.grab_frame
        live.ffmpeg_available = lambda: True
        playback.grab_frame = lambda url, timeout=25, width=None: (b"", "404 Not Found")  # no frame
        try:
            st, b = self.req("GET", f"/playback?device={dev['id']}&ch=101&time=2026-07-25T14:00:00")
            self.assertEqual(st, 502)
            self.assertIn(b"404", b)
        finally:
            live.ffmpeg_available, playback.grab_frame = orig_av, orig_grab

    def test_playback_bad_time_400(self):
        _, dev = self.add_device()
        orig = live.ffmpeg_available
        live.ffmpeg_available = lambda: True
        try:
            st, _ = self.req("GET", f"/playback?device={dev['id']}&ch=101&time=nope")
            self.assertEqual(st, 400)
        finally:
            live.ffmpeg_available = orig

    def test_events_route_lists_motion(self):
        _, dev = self.add_device()
        st, b = self.req("GET", f"/events?device={dev['id']}&ch=101&hours=24")
        self.assertEqual(st, 200)
        data = json.loads(b)
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["seconds"], 17)
        self.assertEqual(data["events"][0]["start"], "20260726T080134Z")
        st2, _ = self.req("GET", "/events?device=nope")
        self.assertEqual(st2, 404)

    def test_clip_streams_mp4(self):
        _, dev = self.add_device()
        orig_av, orig_proc = recordings.ffmpeg_available, recordings.clip_process
        recordings.ffmpeg_available = lambda: True
        recordings.clip_process = lambda cfg, ch, start, end: FakeProc(b"MP4CLIPDATA")  # noqa
        try:
            st, b = self.req("GET", f"/clip?device={dev['id']}&ch=101"
                                    "&start=20260726T080134Z&end=20260726T080151Z")
            self.assertEqual(st, 200)
            self.assertIn(b"MP4CLIPDATA", b)
        finally:
            recordings.ffmpeg_available, recordings.clip_process = orig_av, orig_proc

    def test_clip_download_sends_attachment_filename(self):
        _, dev = self.add_device()
        orig_av, orig_proc = recordings.ffmpeg_available, recordings.clip_process
        recordings.ffmpeg_available = lambda: True
        recordings.clip_process = lambda cfg, ch, start, end: FakeProc(b"MP4CLIPDATA")  # noqa
        try:
            st, h = self.req_headers(f"/clip?device={dev['id']}&ch=101"
                                     "&start=20260726T080134Z&end=20260726T080151Z&download=1")
            self.assertEqual(st, 200)
            self.assertIn("attachment", h["Content-Disposition"])
            self.assertIn("T-ch101-20260726T080134Z.mp4", h["Content-Disposition"])
        finally:
            recordings.ffmpeg_available, recordings.clip_process = orig_av, orig_proc

    def test_clip_while_another_plays_fails_fast(self):
        """A clip requested while one is playing must answer 503, not hang: the old
        unbounded wait made the ⬇ Download button look dead and then delivered every
        queued file at once when the player closed."""
        _, dev = self.add_device()
        orig_av, orig_proc = recordings.ffmpeg_available, recordings.clip_process
        orig_wait = server._CLIP_SESSION_WAIT
        recordings.ffmpeg_available = lambda: True
        recordings.clip_process = lambda cfg, ch, start, end: FakeProc(b"MP4CLIPDATA")  # noqa
        server._CLIP_SESSION_WAIT = 0.2
        try:
            with playback.rtsp_priority():   # stands in for the browser's open <video>
                st, b = self.req("GET", f"/clip?device={dev['id']}&ch=101"
                                        "&start=20260726T080134Z&end=20260726T080151Z&download=1")
            self.assertEqual(st, 503)
            self.assertIn(b"busy", b.lower())
        finally:
            server._CLIP_SESSION_WAIT = orig_wait
            recordings.ffmpeg_available, recordings.clip_process = orig_av, orig_proc

    def test_playback_download_sends_attachment_filename(self):
        _, dev = self.add_device()
        orig_av, orig_grab = live.ffmpeg_available, playback.grab_frame
        live.ffmpeg_available = lambda: True
        playback.grab_frame = lambda url, timeout=25, width=None: (FAKE_JPEG, "")
        try:
            st, h = self.req_headers(
                f"/playback?device={dev['id']}&ch=101&time=2026-07-25T14:00:00&download=1")
            self.assertEqual(st, 200)
            self.assertIn("attachment", h["Content-Disposition"])
            self.assertTrue(h["Content-Disposition"].endswith('.jpg"'))
        finally:
            live.ffmpeg_available, playback.grab_frame = orig_av, orig_grab

    def test_watch_page_and_info_expose_no_credentials(self):
        _, dev = self.add_device()
        st, body = self.req("GET", "/watch")
        self.assertEqual(st, 200)
        self.assertIn(b"<video", body)
        st2, info = self.req("GET", f"/watch/info?device={dev['id']}&ch=101")
        self.assertEqual(st2, 200)
        self.assertEqual(json.loads(info)["name"], "T")
        self.assertNotIn(b"secret", info)          # the share flow never leaks the password
        self.assertNotIn(b"rtsp://", info)
        st3, _ = self.req("GET", "/watch/info?device=gone")
        self.assertEqual(st3, 404)

    def test_clip_requires_ffmpeg(self):
        _, dev = self.add_device()
        orig = recordings.ffmpeg_available
        recordings.ffmpeg_available = lambda: False
        try:
            st, b = self.req("GET", f"/clip?device={dev['id']}&ch=101&start=a&end=b")
            self.assertEqual(st, 503)
            self.assertIn(b"ffmpeg", b)
        finally:
            recordings.ffmpeg_available = orig

    def test_playback_requires_ffmpeg(self):
        _, dev = self.add_device()
        orig = live.ffmpeg_available
        live.ffmpeg_available = lambda: False
        try:
            st, b = self.req("GET", f"/playback?device={dev['id']}&ch=101&time=2026-07-25T14:00:00")
            self.assertEqual(st, 503)
            self.assertIn(b"ffmpeg", b)
        finally:
            live.ffmpeg_available = orig

    def test_live_requires_ffmpeg(self):
        _, dev = self.add_device()
        orig = live.ffmpeg_available
        live.ffmpeg_available = lambda: False
        try:
            st, b = self.req("GET", f"/live?device={dev['id']}&ch=101")
            self.assertEqual(st, 503)
            self.assertIn(b"ffmpeg", b)
        finally:
            live.ffmpeg_available = orig


class RunGuiTest(unittest.TestCase):
    """A detached container has no display: CV_NO_BROWSER must suppress the
    browser launch, and only that — the server still comes up either way."""

    def _run(self, env_value):
        opened = []
        stub = type("Stub", (), {"serve_forever": lambda self: None,
                                 "shutdown": lambda self: None})
        orig = (server.Server, server.webbrowser.open, config.load, events.start_all)
        server.Server = lambda addr, handler: stub()
        server.webbrowser.open = opened.append
        config.load = lambda: None
        events.start_all = lambda: None
        if env_value is None:
            os.environ.pop("CV_NO_BROWSER", None)
        else:
            os.environ["CV_NO_BROWSER"] = env_value
        try:
            with contextlib.redirect_stdout(io.StringIO()):   # keep the banner out of test output
                server.run_gui()
        finally:
            server.Server, server.webbrowser.open, config.load, events.start_all = orig
            os.environ.pop("CV_NO_BROWSER", None)
        return opened

    def test_browser_opens_by_default(self):
        self.assertEqual(len(self._run(None)), 1)

    def test_no_browser_env_suppresses_launch(self):
        self.assertEqual(self._run("1"), [])


if __name__ == "__main__":
    unittest.main()
