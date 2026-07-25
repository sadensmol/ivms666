"""End-to-end HTTP tests through the real request handler (camera layer faked)."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from cameraviewer import config, live, server
from tests.helpers import (
    FakeCamera, FakeProc, FAKE_JPEG, OK_RESP, all_on_gridmap, motion_xml, video_inputs_xml)


def camera_handler(method, path, body):
    if path == "/ISAPI/System/Video/inputs/channels":
        return ("application/xml", video_inputs_xml(2))
    if "/picture" in path:
        return ("image/jpeg", FAKE_JPEG)
    if path.endswith("/motionDetection"):
        if method == "GET":
            return ("application/xml", motion_xml(all_on_gridmap(), sensitivity=50))
        return ("application/xml", OK_RESP)
    raise AssertionError("unexpected camera path: " + path)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = config.CONFIG_PATH
        config.CONFIG_PATH = os.path.join(self.tmp, "cfg.json")
        config._state = {"devices": []}

        self.fake = FakeCamera(camera_handler)
        self.fake.__enter__()

        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fake.__exit__()
        config.CONFIG_PATH = self._orig_path
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

    def add_device(self):
        st, b = self.req("POST", "/devices",
                         {"name": "T", "host": "1.1.1.1", "port": "8001",
                          "user": "admin", "password": "secret"})
        return st, json.loads(b)

    # --- tests -------------------------------------------------------------
    def test_index_and_appjs_served(self):
        st, b = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Camera Viewer", b)
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
            self.assertIn(b"Connection refused", b)
        finally:
            live.ffmpeg_available, live.open_mjpeg = orig_avail, orig_open

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


if __name__ == "__main__":
    unittest.main()
