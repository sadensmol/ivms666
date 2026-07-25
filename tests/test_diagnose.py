"""Diagnose the motion->email->UI pipeline, and auto-fix the linkage gaps."""

import unittest

from cameraviewer import camera, diagnose
from tests.helpers import FakeCamera, OK_RESP, all_on_gridmap, motion_xml, video_inputs_xml


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
        self.puts = []
        self.fake = FakeCamera(self._handler)
        self.fake.__enter__()

    def tearDown(self):
        self.fake.__exit__()

    def _handler(self, method, path, body):
        if path == "/ISAPI/System/Video/inputs/channels":
            return ("application/xml", video_inputs_xml(2))            # inputs 1,2
        if path.endswith("/motionDetection"):
            return ("application/xml", motion_xml(all_on_gridmap(), sensitivity=3))
        if path.startswith("/ISAPI/Event/triggers/VMD-"):
            n = path.rsplit("-", 1)[1]
            if method == "PUT":
                self.puts.append((n, body.decode()))
                # reflect the write so a re-diagnose sees the new linkage
                import re
                self.methods[n] = re.findall(r"<notificationMethod>(\w+)</notificationMethod>", body.decode())
                return ("application/xml", OK_RESP)
            return ("application/xml", trigger_xml(self.methods.get(n, [])))
        if path.startswith("/ISAPI/System/Network/mailing"):
            return ("application/xml", mailing_xml())
        raise AssertionError("unexpected path: " + path)

    def _cfg(self):
        return {"host": "1.1.1.1", "port": "80", "user": "u", "password": "p"}

    # --- tests -------------------------------------------------------------
    def test_diagnose_flags_missing_center(self):
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
        result = diagnose.apply_fixes(self._cfg())
        # a PUT that adds center reached input 1 (not input 2, already complete)
        self.assertTrue(any(n == "1" and "center" in body for n, body in self.puts))
        self.assertFalse(any(n == "2" for n, _ in self.puts))
        added = {f["input"]: f["added"] for f in result["fixes"]}
        self.assertEqual(added, {"1": ["center"]})
        # post-fix report is clean
        self.assertFalse(result["report"]["fixable"])

    def test_hidden_channels_are_skipped(self):
        rep = diagnose.diagnose(self._cfg(), hidden=["201"])  # hide Camera 02 (id 201)
        self.assertEqual([c["input"] for c in rep["channels"]], ["1"])
        # and fixes skip it too
        self.methods["1"] = ["record"]
        res = diagnose.apply_fixes(self._cfg(), hidden=["201"])
        self.assertFalse(any(n == "2" for n, _ in self.puts))
        self.assertTrue(any(n == "1" for n, _ in self.puts))

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
