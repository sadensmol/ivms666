"""Diagnose the motion->email->UI pipeline, and auto-fix the linkage gaps."""

import re
import unittest
import urllib.error

from cameraviewer import camera, diagnose
from tests.helpers import (
    FakeCamera, OK_RESP, all_on_gridmap, motion_xml, record_track_xml,
    stream_caps_xml, stream_channel_xml, time_xml, video_inputs_xml)


def trigger_xml(methods):
    """A VMD EventTrigger whose notification list has the given methods."""
    items = "".join(
        f"<EventTriggerNotification><id>{m}</id>"
        f"<notificationMethod>{m}</notificationMethod></EventTriggerNotification>"
        for m in methods
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>'
        '<EventTrigger version="1.0" xmlns="http://www.std-cgi.com/ver20/XMLSchema">'
        "<id>VMD-1</id><eventType>VMD</eventType><videoInputChannelID>1</videoInputChannelID>"
        f'<EventTriggerNotificationList version="1.0">{items}</EventTriggerNotificationList>'
        "</EventTrigger>"
    ).encode()


def mailing_xml(receivers=2, server="smtp.example.com"):
    recv = "".join(f"<receiver><receiverAddress>a{i}@x.com</receiverAddress></receiver>"
                   for i in range(receivers))
    return (f'<mailing><hostName>{server}</hostName>'
            f"<receiverList>{recv}</receiverList></mailing>").encode()


class DiagnoseTest(unittest.TestCase):
    def setUp(self):
        # per-channel notification methods the fake DVR currently reports
        self.methods = {"1": ["record", "email"], "2": ["record", "email", "center"]}
        self.rec = {"mode": "CMR", "pre": 5, "post": 5, "enable": True}  # fake recording state
        self.res = {"w": 1920, "h": 1080}                # fake main-stream resolution
        self.reject_widths = ()  # widths the fake DVR 500s on (simulates a capped channel)
        self.disabled = ()      # input ids rendered as NO VIDEO (empty slots)
        self.time_skew = 0      # DVR clock offset from real local time, seconds
        self.puts = []
        self.track_puts = []
        self.stream_puts = []
        self.time_puts = []
        self.fake = FakeCamera(self._handler)
        self.fake.__enter__()

    def tearDown(self):
        self.fake.__exit__()

    def _handler(self, method, path, body):
        if path == "/ISAPI/System/Video/inputs/channels":
            return ("application/xml", video_inputs_xml(2, disabled=self.disabled))
        if path == "/ISAPI/System/time":
            if method == "PUT":
                self.time_puts.append(body.decode())
                self.time_skew = 0                       # write corrects the clock
                return ("application/xml", OK_RESP)
            return ("application/xml", time_xml(self.time_skew))
        if path.endswith("/motionDetection"):
            return ("application/xml", motion_xml(all_on_gridmap(), sensitivity=3))
        if path.startswith("/ISAPI/Event/triggers/VMD-"):
            n = path.rsplit("-", 1)[1]
            if method == "PUT":
                self.puts.append((n, body.decode()))
                # reflect the write so a re-diagnose sees the new linkage
                self.methods[n] = re.findall(r"<notificationMethod>(\w+)</notificationMethod>", body.decode())
                return ("application/xml", OK_RESP)
            return ("application/xml", trigger_xml(self.methods.get(n, [])))
        if path.startswith("/ISAPI/System/Network/mailing"):
            return ("application/xml", mailing_xml())
        if path.startswith("/ISAPI/ContentMgmt/record/tracks/"):
            if method == "PUT":
                b = body.decode()
                self.track_puts.append((path, b))
                mode = re.search(r"<ActionRecordingMode>(\w+)</ActionRecordingMode>", b)
                pre = re.search(r"<PreRecordTimeSeconds>(\d+)", b)
                post = re.search(r"<PostRecordTimeSeconds>(\d+)", b)
                en = re.search(r"<Enable>(\w+)</Enable>", b)
                if mode:
                    self.rec["mode"] = mode.group(1)
                if pre:
                    self.rec["pre"] = int(pre.group(1))
                if post:
                    self.rec["post"] = int(post.group(1))
                if en:
                    self.rec["enable"] = en.group(1).lower() == "true"
                return ("application/xml", OK_RESP)
            return ("application/xml", record_track_xml(
                self.rec["mode"], self.rec["pre"], self.rec["post"], enable=self.rec["enable"]))
        if path.startswith("/ISAPI/Streaming/channels/"):
            if path.endswith("/capabilities"):
                return ("application/xml", stream_caps_xml())
            if method == "PUT":
                b = body.decode()
                self.stream_puts.append((path, b))
                w = re.search(r"<videoResolutionWidth>(\d+)", b)
                h = re.search(r"<videoResolutionHeight>(\d+)", b)
                if w and int(w.group(1)) in self.reject_widths:
                    raise urllib.error.HTTPError(path, 500, "Device Error", None, None)
                if w:
                    self.res["w"] = int(w.group(1))
                if h:
                    self.res["h"] = int(h.group(1))
                return ("application/xml", OK_RESP)
            return ("application/xml", stream_channel_xml(self.res["w"], self.res["h"]))
        raise AssertionError("unexpected path: " + path)

    def _cfg(self):
        return {"host": "1.1.1.1", "port": "80", "user": "u", "password": "p"}

    # --- tests -------------------------------------------------------------
    def test_diagnose_flags_missing_center(self):
        self.rec = {"mode": "MOTION", "pre": 10, "post": 10}  # isolate linkage checks
        rep = diagnose.diagnose(self._cfg())
        self.assertTrue(rep["smtp"]["ok"])
        by_input = {c["input"]: c for c in rep["channels"]}
        # input 1: has email, missing center -> a fixable no_center issue
        codes1 = [i["code"] for i in by_input["1"]["issues"]]
        self.assertIn("no_center", codes1)
        self.assertNotIn("no_email", codes1)
        # input 2: fully linked -> no issues
        self.assertEqual(by_input["2"]["issues"], [])
        self.assertTrue(rep["fixable"])

    def test_apply_fixes_adds_center_and_clears_issue(self):
        self.rec = {"mode": "MOTION", "pre": 10, "post": 10}  # isolate linkage fix
        result = diagnose.apply_fixes(self._cfg())
        # a PUT that adds center reached input 1 (not input 2, already complete)
        self.assertTrue(any(n == "1" and "center" in body for n, body in self.puts))
        self.assertFalse(any(n == "2" for n, _ in self.puts))
        added = {f["input"]: f["added"] for f in result["fixes"]}
        self.assertEqual(added, {"1": ["center"]})
        # post-fix report is clean
        self.assertFalse(result["report"]["fixable"])

    def test_flags_recording_not_motion_and_low_prepost(self):
        rep = diagnose.diagnose(self._cfg())
        c1 = next(c for c in rep["channels"] if c["input"] == "1")
        codes = [i["code"] for i in c1["issues"]]
        self.assertIn("rec_not_motion", codes)   # fake DVR is CMR
        self.assertIn("pre_record_low", codes)   # 5 < 10
        self.assertIn("post_record_low", codes)  # 5 < 10
        self.assertEqual(c1["record_mode"], "cmr")
        self.assertEqual(c1["pre_record"], 5)

    def test_fix_sets_motion_recording_10s(self):
        result = diagnose.apply_fixes(self._cfg())
        self.assertTrue(self.track_puts)  # a track PUT happened
        body = self.track_puts[0][1]
        self.assertIn("<ActionRecordingMode>MOTION</ActionRecordingMode>", body)
        self.assertNotIn("<ActionRecordingMode>CMR</ActionRecordingMode>", body)
        self.assertIn("<PreRecordTimeSeconds>10</PreRecordTimeSeconds>", body)
        self.assertIn("<PostRecordTimeSeconds>10</PostRecordTimeSeconds>", body)
        # fixes summary mentions the recording change
        added = [a for f in result["fixes"] for a in f["added"]]
        self.assertTrue(any("motion-rec" in a for a in added))

    def test_recording_ok_when_motion_and_10s(self):
        self.rec = {"mode": "MOTION", "pre": 10, "post": 10}
        rep = diagnose.diagnose(self._cfg())
        codes = [i["code"] for c in rep["channels"] for i in c["issues"]]
        self.assertNotIn("rec_not_motion", codes)
        self.assertNotIn("pre_record_low", codes)
        self.assertNotIn("post_record_low", codes)

    def test_flags_below_hd_and_fixes_to_max(self):
        self.res = {"w": 704, "h": 576}  # D1, below HD
        rep = diagnose.diagnose(self._cfg())
        c1 = next(c for c in rep["channels"] if c["input"] == "1")
        self.assertIn("rec_quality_low", [i["code"] for i in c1["issues"]])
        self.assertEqual(c1["rec_resolution"], "704x576")
        self.assertEqual(c1["max_resolution"], "1920x1080")
        diagnose.apply_fixes(self._cfg())
        body = self.stream_puts[0][1]
        self.assertIn("<videoResolutionWidth>1920</videoResolutionWidth>", body)
        self.assertIn("<videoResolutionHeight>1080</videoResolutionHeight>", body)

    def test_quality_ok_at_max_resolution(self):
        self.res = {"w": 1920, "h": 1080}  # already Full HD / max
        codes = [i["code"] for c in diagnose.diagnose(self._cfg())["channels"] for i in c["issues"]]
        self.assertNotIn("rec_quality_low", codes)

    def test_720p_is_acceptable_even_if_caps_advertise_more(self):
        # this DVR over-advertises 1080p on a 720p-only channel; 720p is HD -> don't nag
        self.res = {"w": 1280, "h": 720}   # HD, caps say max 1920x1080
        c1 = next(c for c in diagnose.diagnose(self._cfg())["channels"] if c["input"] == "1")
        self.assertNotIn("rec_quality_low", [i["code"] for i in c1["issues"]])
        self.assertEqual(c1["rec_resolution"], "1280x720")

    def test_quality_fix_steps_down_when_top_resolution_rejected(self):
        # device 500s on 1920 (like garage Camera 04) -> fix must fall back to 1280x720
        self.res = {"w": 704, "h": 576}
        self.reject_widths = (1920,)
        diagnose.apply_fixes(self._cfg())
        widths = [re.search(r"<videoResolutionWidth>(\d+)", b).group(1) for _, b in self.stream_puts]
        self.assertIn("1920", widths)      # it tried the advertised max first...
        self.assertIn("1280", widths)      # ...then stepped down to 720p and that stuck
        self.assertEqual(self.res, {"w": 1280, "h": 720})

    def test_hidden_channel_gets_unused_check_not_full_diagnosis(self):
        rep = diagnose.diagnose(self._cfg(), hidden=["201"])  # hide Camera 02 (id 201)
        by_input = {c["input"]: c for c in rep["channels"]}
        # hidden channel: only the wasteful-recording check ran (no motion/email/center)
        self.assertEqual(by_input["2"]["unused"], "hidden")
        self.assertEqual([i["code"] for i in by_input["2"]["issues"]], ["rec_wasteful"])
        # the visible channel is still fully diagnosed
        self.assertNotIn("unused", by_input["1"])

    def test_hidden_channel_recording_turned_off_no_linkage_writes(self):
        diagnose.apply_fixes(self._cfg(), hidden=["201"])
        # recording disabled on the hidden channel's track...
        off = next(b for p, b in self.track_puts if p.endswith("/201"))
        self.assertIn("<Enable>false</Enable>", off)
        # ...and no VMD-trigger (email/center) PUT ever reached the hidden channel
        self.assertFalse(any(n == "2" for n, _ in self.puts))

    def test_no_camera_channel_flags_and_turns_off_recording(self):
        self.disabled = (2,)                       # Camera 02's input = NO VIDEO
        rep = diagnose.diagnose(self._cfg())
        c2 = next(c for c in rep["channels"] if c["input"] == "2")
        self.assertEqual(c2["unused"], "no_camera")
        self.assertEqual([i["code"] for i in c2["issues"]], ["rec_wasteful"])
        self.assertTrue(rep["fixable"])
        diagnose.apply_fixes(self._cfg())
        off = next(b for p, b in self.track_puts if p.endswith("/201"))
        self.assertIn("<Enable>false</Enable>", off)

    def test_no_camera_channel_clean_when_recording_already_off(self):
        self.disabled = (2,)
        self.rec["enable"] = False                 # recording already off -> nothing to fix
        c2 = next(c for c in diagnose.diagnose(self._cfg())["channels"] if c["input"] == "2")
        self.assertEqual(c2["unused"], "no_camera")
        self.assertEqual(c2["issues"], [])
        self.assertFalse(c2["recording_on"])

    def test_clock_ok_when_in_sync(self):
        rep = diagnose.diagnose(self._cfg())       # time_skew defaults to 0
        self.assertTrue(rep["clock"]["ok"])
        self.assertNotIn("fixable", rep["clock"])

    def test_clock_flagged_and_corrected_when_skewed(self):
        self.time_skew = -4 * 3600                 # DVR clock ~4h behind
        rep = diagnose.diagnose(self._cfg())
        self.assertFalse(rep["clock"]["ok"])
        self.assertTrue(rep["clock"]["fixable"])
        self.assertTrue(rep["fixable"])
        result = diagnose.apply_fixes(self._cfg())
        self.assertTrue(self.time_puts)            # the corrected localTime was written
        self.assertIn("<localTime>", self.time_puts[0])
        self.assertTrue(any(f["name"] == "DVR clock" for f in result["fixes"]))
        self.assertTrue(result["report"]["clock"]["ok"])  # re-diagnose is clean

    def test_missing_email_is_fixable(self):
        self.methods["1"] = ["record"]  # no email, no center
        rep = diagnose.diagnose(self._cfg())
        codes = [i["code"] for i in rep["channels"][0]["issues"]]
        self.assertIn("no_email", codes)
        self.assertIn("no_center", codes)
        diagnose.apply_fixes(self._cfg())
        put_body = next(b for n, b in self.puts if n == "1")
        self.assertIn("email", put_body)
        self.assertIn("center", put_body)


if __name__ == "__main__":
    unittest.main()
