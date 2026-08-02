"""Motion event log: time parsing + CMSearch parsing (ffmpeg clip is faked)."""

import re
import time
import unittest
from datetime import datetime, timedelta, timezone

import recordings
from tests.helpers import FakeCamera, cmsearch_result_xml, time_xml


def _span_hours(start_iso, end_iso):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return round((datetime.strptime(end_iso, fmt) - datetime.strptime(start_iso, fmt)).total_seconds() / 3600)


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

    def test_list_events_pages_until_the_dvr_stops_saying_MORE(self):
        # The DVR truncates a search to ~64 matches, answers "MORE", and hands back
        # the OLDEST ones first -- so without paging the NEWEST events are the ones
        # missing (the "event log stops hours ago" bug).
        pages = {
            0: ([("2026-07-26T08:00:00Z", "2026-07-26T08:00:10Z"),
                 ("2026-07-26T09:00:00Z", "2026-07-26T09:00:10Z")], "MORE"),
            2: ([("2026-07-26T16:00:00Z", "2026-07-26T16:00:10Z")], "OK"),
        }
        seen = []

        def handler(method, path, body):
            pos = int(re.search(rb"<searchResultPostion>(\d+)<", body).group(1))
            seen.append(pos)
            spans, status = pages[pos]
            return ("application/xml", cmsearch_result_xml(spans, status))

        with FakeCamera(handler):
            evs = recordings.list_events({"host": "h", "port": "80"}, "101", "a", "b")
        self.assertEqual(seen, [0, 2])
        self.assertEqual(len(evs), 3)
        self.assertEqual(evs[0]["start"], "20260726T160000Z")   # newest page included

    def test_list_events_stops_at_max_results(self):
        def handler(method, path, body):
            return ("application/xml",
                    cmsearch_result_xml([("2026-07-26T08:00:00Z", "2026-07-26T08:00:10Z")], "MORE"))

        with FakeCamera(handler) as cam:
            evs = recordings.list_events({"host": "h", "port": "80"}, "101", "a", "b", max_results=3)
        self.assertEqual(len(evs), 3)      # an always-"MORE" DVR must not loop forever
        self.assertEqual(len(cam.posts), 3)

    def test_dvr_window_is_expressed_in_the_dvrs_own_drifting_clock(self):
        # DVR runs 2h behind real time in +03:00 -> the "last 1h" window must be
        # 2h back in DVR wall-clock terms, or CMSearch trims the newest recording.
        recordings._clock_cache.clear()
        with FakeCamera(lambda m, p, b: ("application/xml", time_xml(skew_secs=-7200))):
            start, end = recordings.dvr_window({"host": "skewed", "port": "80"}, 1)
        real_now = datetime.now(timezone(timedelta(hours=3)))
        dvr_end = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone(timedelta(hours=3)))
        self.assertAlmostEqual((real_now - dvr_end).total_seconds(), 7200, delta=30)
        self.assertEqual(_span_hours(start, end), 1)

    def test_list_events_reports_the_real_instant_as_epoch(self):
        # `time` stays the DVR clock (playback/clip speak it); `epoch` is the real
        # instant so the browser can render it in the VIEWER's timezone.
        recordings._clock_cache.clear()
        dvr = datetime.now(timezone(timedelta(hours=3))) - timedelta(hours=2)  # DVR is 2h slow
        stamp = dvr.strftime("%Y-%m-%dT%H:%M:%SZ")

        def handler(method, path, body):
            if path == "/ISAPI/System/time":
                return ("application/xml", time_xml(skew_secs=-7200))
            return ("application/xml", cmsearch_result_xml([(stamp, stamp)]))

        with FakeCamera(handler):
            evs = recordings.list_events({"host": "skewed2", "port": "80"}, "101", "a", "b")
        self.assertEqual(evs[0]["time"], stamp.replace("Z", ""))    # unchanged DVR clock
        self.assertAlmostEqual(evs[0]["epoch"], time.time(), delta=30)  # ...but the real instant

    def test_list_events_empty(self):
        with FakeCamera(lambda m, p, b: ("application/xml", cmsearch_result_xml([]))):
            self.assertEqual(recordings.list_events({"host": "h", "port": "80"}, "101", "a", "b"), [])


if __name__ == "__main__":
    unittest.main()
