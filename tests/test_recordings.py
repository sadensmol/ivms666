"""Motion event log: time parsing + CMSearch parsing (ffmpeg clip is faked)."""

import unittest

from cameraviewer import recordings
from tests.helpers import FakeCamera, cmsearch_result_xml


class RecordingsTest(unittest.TestCase):
    def test_iso_to_rtsp(self):
        self.assertEqual(recordings._iso_to_rtsp("2026-07-26T08:01:34Z"), "20260726T080134Z")
        self.assertEqual(recordings._iso_to_rtsp("2026-07-26T08:01:34"), "20260726T080134Z")

    def test_seconds_between(self):
        self.assertEqual(recordings._seconds("2026-07-26T08:01:34Z", "2026-07-26T08:01:51Z"), 17)

    def test_list_events_parses_sorts_and_formats(self):
        spans = [("2026-07-26T08:01:34Z", "2026-07-26T08:01:51Z"),   # 17s
                 ("2026-07-26T09:18:25Z", "2026-07-26T09:18:41Z")]   # 16s, later
        def handler(method, path, body):
            if path == "/ISAPI/ContentMgmt/search":
                self.assertEqual(method, "POST")
                self.assertIn(b"<trackID>101</trackID>", body)
                return ("application/xml", cmsearch_result_xml(spans))
            raise AssertionError("unexpected path: " + path)

        with FakeCamera(handler):
            evs = recordings.list_events({"host": "h", "port": "80"}, "101",
                                         "2026-07-26T00:00:00Z", "2026-07-26T23:59:59Z")
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["start"], "20260726T091825Z")   # newest first
        self.assertEqual(evs[0]["time"], "2026-07-26T09:18:25")  # display (no Z)
        self.assertEqual(evs[1]["seconds"], 17)
        self.assertEqual(evs[1]["end"], "20260726T080151Z")

    def test_list_events_empty(self):
        with FakeCamera(lambda m, p, b: ("application/xml", cmsearch_result_xml([]))):
            self.assertEqual(recordings.list_events({"host": "h", "port": "80"}, "101", "a", "b"), [])


if __name__ == "__main__":
    unittest.main()
