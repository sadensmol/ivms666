"""Channel discovery and snapshot fetching."""

import unittest
import urllib.error

import camera
from tests.helpers import FakeCamera, FAKE_JPEG, streaming_channels_xml, video_inputs_xml

CFG = {"host": "h", "port": "1", "user": "u", "password": "p"}


class DiscoverTest(unittest.TestCase):
    def test_discovers_video_input_channels(self):
        def handler(method, path, body):
            if path == "/ISAPI/System/Video/inputs/channels":
                return ("application/xml", video_inputs_xml(3))
            raise AssertionError("should not reach fallbacks")

        with FakeCamera(handler):
            chans = camera.discover_channels(CFG)
        self.assertEqual([c["id"] for c in chans], ["101", "201", "301"])
        self.assertEqual([c["input"] for c in chans], ["1", "2", "3"])
        self.assertEqual(chans[0]["name"], "Camera 01")

    def test_falls_back_to_streaming_channels(self):
        def handler(method, path, body):
            if path == "/ISAPI/System/Video/inputs/channels":
                raise urllib.error.URLError("nope")
            if path == "/ISAPI/Streaming/channels":
                return ("application/xml", streaming_channels_xml(["101", "102", "201", "202"]))
            raise AssertionError("unexpected path " + path)

        with FakeCamera(handler):
            chans = camera.discover_channels(CFG)
        # keeps only main streams (ids ending in 01)
        self.assertEqual([c["id"] for c in chans], ["101", "201"])
        self.assertEqual([c["input"] for c in chans], ["1", "2"])

    def test_falls_back_to_probing(self):
        def handler(method, path, body):
            if "channels" in path and "picture" not in path:
                raise urllib.error.URLError("no listing")
            # picture endpoint: only channels 1 and 3 respond
            if path.startswith("/ISAPI/Streaming/channels/101/picture") or \
               path.startswith("/ISAPI/Streaming/channels/301/picture"):
                return ("image/jpeg", FAKE_JPEG)
            raise urllib.error.HTTPError(path, 404, "no", {}, None)

        with FakeCamera(handler):
            chans = camera.discover_channels(CFG)
        self.assertEqual([c["id"] for c in chans], ["101", "301"])


class SnapshotTest(unittest.TestCase):
    def test_requests_high_resolution(self):
        seen = []

        def handler(method, path, body):
            seen.append(path)
            return ("image/jpeg", FAKE_JPEG)

        with FakeCamera(handler):
            ctype, data = camera.fetch_snapshot(CFG, "101", "1280x720")
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(data, FAKE_JPEG)
        self.assertIn("videoResolutionWidth=1280", seen[0])
        self.assertIn("videoResolutionHeight=720", seen[0])

    def test_falls_back_when_resolution_rejected(self):
        def handler(method, path, body):
            if "videoResolutionWidth" in path:
                raise urllib.error.HTTPError(path, 400, "bad size", {}, None)
            return ("image/jpeg", FAKE_JPEG)

        with FakeCamera(handler) as fake:
            ctype, data = camera.fetch_snapshot(CFG, "101", "1920x1080")
        self.assertEqual(data, FAKE_JPEG)
        # second attempt hit the plain endpoint
        self.assertTrue(fake.gets[-1].endswith("/ISAPI/Streaming/channels/101/picture"))

    def test_default_resolution_uses_plain_endpoint(self):
        with FakeCamera(lambda m, p, b: ("image/jpeg", FAKE_JPEG)) as fake:
            camera.fetch_snapshot(CFG, "201")
        self.assertEqual(fake.gets[-1], "/ISAPI/Streaming/channels/201/picture")


class XmlSafetyTest(unittest.TestCase):
    def test_rejects_doctype(self):
        with self.assertRaises(ValueError):
            camera.parse_xml(b'<!DOCTYPE x [<!ENTITY a "b">]><r/>')


if __name__ == "__main__":
    unittest.main()
