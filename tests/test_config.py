"""Device store: CRUD, password masking, and persistence."""

import json
import os
import stat
import tempfile
import unittest

from cameraviewer import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = config.CONFIG_PATH
        config.CONFIG_PATH = os.path.join(self.tmp, "cfg.json")
        config._state = {"devices": []}

    def tearDown(self):
        config.CONFIG_PATH = self._orig_path
        config._state = {"devices": []}

    def _add(self, **kw):
        base = {"name": "NVR", "host": "1.2.3.4", "port": "8001",
                "user": "admin", "password": "secret"}
        base.update(kw)
        return config.add_device(base)

    def test_add_and_mask(self):
        masked = self._add()
        self.assertTrue(masked["id"])
        self.assertEqual(masked["host"], "1.2.3.4")
        self.assertTrue(masked["hasPassword"])
        self.assertNotIn("password", masked)  # never exposed

    def test_list_devices_hides_password(self):
        self._add()
        listed = config.list_devices()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("password", listed[0])

    def test_get_cfg_includes_password(self):
        d = self._add()
        cfg = config.get_cfg(d["id"])
        self.assertEqual(cfg["password"], "secret")

    def test_groups_create_list_delete_and_membership(self):
        d = self._add()
        self.assertEqual(config.list_groups(), [])
        config.create_group("Backyard")
        self.assertEqual(config.list_groups(), ["Backyard"])          # empty group persists
        # assign a device to it (also shows in the masked view)
        self.assertEqual(config.update_device(d["id"], {"group": "Backyard"})["group"], "Backyard")
        # a group referenced by a device is listed even if not in the store
        config.update_device(d["id"], {"group": "Adhoc"})
        self.assertIn("Adhoc", config.list_groups())
        # deleting a group un-assigns its members (devices stay)
        self.assertEqual(config.delete_group("Adhoc"), [])          # nothing deleted
        self.assertNotIn("Adhoc", config.list_groups())
        self.assertEqual(config.list_devices()[0]["group"], "")

    def test_delete_group_cascade_removes_member_devices(self):
        d = self._add()
        config.update_device(d["id"], {"group": "External"})
        deleted = config.delete_group("External", delete_devices=True)
        self.assertEqual(deleted, [d["id"]])                        # returns removed ids
        self.assertEqual(config.list_devices(), [])                 # member device is gone
        self.assertNotIn("External", config.list_groups())

    def test_inactive_channels_persist_and_mask(self):
        d = self._add()
        self.assertEqual(config.list_devices()[0]["inactive"], [])   # active by default
        out = config.update_device(d["id"], {"inactive": ["101", "301"]})
        self.assertEqual(out["inactive"], ["101", "301"])
        with open(config.CONFIG_PATH) as f:
            self.assertEqual(json.load(f)["devices"][0]["inactive"], ["101", "301"])
        # re-activating all clears it
        self.assertEqual(config.update_device(d["id"], {"inactive": []})["inactive"], [])

    def test_audio_only_flag_persists_and_masks(self):
        d = self._add()
        self.assertFalse(config.list_devices()[0]["audioOnly"])   # video by default
        out = config.update_device(d["id"], {"audio_only": True})
        self.assertTrue(out["audioOnly"])
        with open(config.CONFIG_PATH) as f:
            self.assertTrue(json.load(f)["devices"][0]["audio_only"])
        # editing/re-probing clears it (video may have returned)
        self.assertFalse(config.update_device(d["id"], {"audio_only": False})["audioOnly"])

    def test_add_rtsp_stream_parses_url_into_fields_and_hides_password(self):
        url = "rtsp://admin:sesame@cam.local:8554/live/0"
        masked = config.add_device({"rtsp_url": url})
        self.assertEqual(masked["kind"], "rtsp")
        self.assertFalse(masked["isapiEnabled"])                 # RTSP-only
        self.assertEqual(masked["host"], "cam.local")            # all parsed from the URL
        self.assertEqual(masked["rtspPort"], "8554")
        self.assertEqual(masked["user"], "admin")
        self.assertEqual(masked["path"], "/live/0")              # verbatim path = the camera
        self.assertEqual(masked["name"], "cam.local/live/0")     # name defaults to host + path
        # the password never appears in any browser-facing view (and no opaque URL is stored)
        self.assertNotIn("sesame", json.dumps(masked))
        self.assertNotIn("sesame", json.dumps(config.list_devices()))
        cfg = config.get_cfg(masked["id"])
        self.assertEqual((cfg["user"], cfg["password"], cfg["path"]), ("admin", "sesame", "/live/0"))

    def test_isapi_off_makes_device_rtsp_only(self):
        d = config.add_device({"name": "Cam", "host": "h", "port": "80", "isapi_enabled": False,
                               "path": "/live", "rtsp_port": "10554", "agentgreen_port": "8090"})
        self.assertEqual(d["kind"], "rtsp")           # derived from isapi_enabled
        self.assertFalse(d["isapiEnabled"])
        self.assertEqual(d["path"], "/live")
        self.assertEqual((d["rtspPort"], d["agentgreenPort"]), ("10554", "8090"))
        # toggling ISAPI back on makes it a DVR again
        self.assertEqual(config.update_device(d["id"], {"isapi_enabled": True})["kind"], "dvr")

    def test_agentgreen_port_defaults_to_8090_and_disabled(self):
        d = self._add()  # a plain DVR
        self.assertEqual(d["agentgreenPort"], "8090")
        self.assertFalse(d["agentgreenEnabled"])   # off by default — not everyone uses it
        self.assertTrue(config.update_device(d["id"], {"agentgreen_enabled": True})["agentgreenEnabled"])

    def test_rtsp_url_parsing_handles_special_chars_in_password(self):
        # userinfo ends at the LAST '@' before the path — '@', ':' and '#' in the
        # password must not be mistaken for the host boundary.
        cases = {
            "rtsp://admin:p@ss@cam.local:8554/live":  ("cam.local", "8554", "admin", "p@ss", "/live"),
            "rtsp://admin:a:b:c@10.0.0.5/stream":     ("10.0.0.5", "554", "admin", "a:b:c", "/stream"),
            "rtsp://user:Ap#ry123@host.example:554/1": ("host.example", "554", "user", "Ap#ry123", "/1"),
        }
        for url, (host, port, user, pw, path) in cases.items():
            p = config._parse_rtsp_url(url)
            self.assertEqual((p["host"], p["rtsp_port"], p["user"], p["password"], p["path"]),
                             (host, port, user, pw, path), url)

    def test_rtsp_device_persists_and_edit_keeps_fields(self):
        d = config.add_device({"rtsp_url": "rtsp://u:p@h:554/s"})
        with open(config.CONFIG_PATH) as f:
            saved = json.load(f)["devices"][0]
            self.assertEqual(saved["kind"], "rtsp")
            self.assertEqual(saved["path"], "/s")
            self.assertNotIn("rtsp_url", saved)              # stored as fields, not an opaque URL
        # renaming keeps the parsed fields
        config.update_device(d["id"], {"name": "Backyard"})
        cfg = config.get_cfg(d["id"])
        self.assertEqual((cfg["host"], cfg["path"], cfg["password"]), ("h", "/s", "p"))
        self.assertEqual(config.list_devices()[0]["name"], "Backyard")
        # re-pasting a URL re-parses every field
        config.update_device(d["id"], {"rtsp_url": "rtsp://u2:p2@h2:5/x"})
        cfg = config.get_cfg(d["id"])
        self.assertEqual((cfg["host"], cfg["rtsp_port"], cfg["user"], cfg["path"]), ("h2", "5", "u2", "/x"))
        self.assertIsNone(config.get_cfg("nope"))

    def test_update_keeps_password_when_blank(self):
        d = self._add()
        config.update_device(d["id"], {"name": "Renamed", "password": ""})
        self.assertEqual(config.get_cfg(d["id"])["password"], "secret")
        self.assertEqual(config.list_devices()[0]["name"], "Renamed")

    def test_update_changes_password_when_provided(self):
        d = self._add()
        config.update_device(d["id"], {"password": "new"})
        self.assertEqual(config.get_cfg(d["id"])["password"], "new")

    def test_update_hidden_setup(self):
        d = self._add()
        config.update_device(d["id"], {"hidden": ["301", "401"]})
        self.assertEqual(config.list_devices()[0]["hidden"], ["301", "401"])

    def test_update_unknown_returns_none(self):
        self.assertIsNone(config.update_device("missing", {"name": "x"}))

    def test_delete(self):
        d = self._add()
        self.assertTrue(config.delete_device(d["id"]))
        self.assertEqual(config.list_devices(), [])
        self.assertFalse(config.delete_device(d["id"]))  # already gone

    def test_write_read_devices_file_roundtrip(self):
        path = os.path.join(self.tmp, "scan.json")
        entries = [{"name": "cam", "host": "1.2.3.4", "port": "80",
                    "user": "admin", "password": "p", "rtsp_port": "554"}]
        config.write_devices_file(path, entries)
        with open(path) as f:
            self.assertEqual(json.load(f), {"devices": entries})
        self.assertEqual(config.read_devices_file(path), entries)

    def test_read_devices_file_accepts_bare_list(self):
        path = os.path.join(self.tmp, "bare.json")
        with open(path, "w") as f:
            json.dump([{"host": "5.6.7.8"}], f)
        self.assertEqual(config.read_devices_file(path), [{"host": "5.6.7.8"}])

    def test_import_devices_adds_and_persists(self):
        added, skipped = config.import_devices(
            [{"host": "1.2.3.4", "user": "admin", "password": "p", "rtsp_port": "554"}])
        self.assertEqual((added, skipped), (1, 0))
        listed = config.list_devices()
        self.assertEqual(listed[0]["host"], "1.2.3.4")
        self.assertEqual(listed[0]["rtspPort"], "554")
        self.assertEqual(config.get_cfg(listed[0]["id"])["password"], "p")
        # written to disk with the default HTTP port filled in
        with open(config.CONFIG_PATH) as f:
            self.assertEqual(json.load(f)["devices"][0]["port"], "80")

    def test_import_devices_dedupes_by_host_port_user(self):
        entry = {"host": "1.2.3.4", "user": "admin", "password": "p", "rtsp_port": "554"}
        config.import_devices([entry])
        added, skipped = config.import_devices([entry])  # same host+port+user
        self.assertEqual((added, skipped), (0, 1))
        self.assertEqual(len(config.list_devices()), 1)

    def test_import_devices_skips_hostless(self):
        added, skipped = config.import_devices([{"user": "admin"}])
        self.assertEqual((added, skipped), (0, 1))

    def test_import_rtsp_url_entry_creates_rtsp_device(self):
        url = "rtsp://admin:letmein@9.9.9.9:8554/Streaming/Channels/101"
        self.assertEqual(config.import_devices([{"rtsp_url": url}]), (1, 0))
        d = config.list_devices()[0]
        self.assertEqual(d["kind"], "rtsp")
        self.assertFalse(d["isapiEnabled"])
        self.assertEqual((d["host"], d["rtspPort"], d["path"]),
                         ("9.9.9.9", "8554", "/Streaming/Channels/101"))
        self.assertEqual(config.get_cfg(d["id"])["password"], "letmein")
        self.assertEqual(config.import_devices([{"rtsp_url": url}]), (0, 1))  # idempotent

    def test_merge_devices_file_appends_without_deleting(self):
        path = os.path.join(self.tmp, "out.json")
        config.merge_devices_file(path, [{"rtsp_url": "rtsp://h:554/a"}])
        config.merge_devices_file(path, [{"rtsp_url": "rtsp://h:554/a"},     # dup -> kept once
                                         {"rtsp_url": "rtsp://h:554/b"}])     # new -> appended
        urls = [e["rtsp_url"] for e in config.read_devices_file(path)]
        self.assertEqual(urls, ["rtsp://h:554/a", "rtsp://h:554/b"])

    def test_merge_devices_file_creates_missing_file(self):
        path = os.path.join(self.tmp, "nope.json")
        self.assertEqual(len(config.merge_devices_file(path, [{"rtsp_url": "rtsp://x:1/y"}])), 1)
        self.assertTrue(os.path.exists(path))

    def test_default_scan_string_forms_become_lists(self):
        orig = config._load_defaults
        config._load_defaults = lambda: {"scan": {
            "range": "10.0.0.0/24", "ports": "554,8554",
            "logins": ["admin", " root "], "passwords": "12345, admin ,"}}
        try:
            s = config.default_scan()
        finally:
            config._load_defaults = orig
        self.assertEqual(s["range"], ["10.0.0.0/24"])              # single string -> 1-item list
        self.assertEqual(s["ports"], ["554", "8554"])             # comma string -> list
        self.assertEqual(s["logins"], ["admin", "root"])          # trimmed
        self.assertEqual(s["passwords"], ["12345", "admin"])       # csv string + blanks dropped

    def test_default_scan_json_lists(self):
        orig = config._load_defaults
        config._load_defaults = lambda: {"scan": {
            "range": ["192.168.1.0/24", "192.168.2.0/24"], "ports": ["554", "8554"]}}
        try:
            s = config.default_scan()
        finally:
            config._load_defaults = orig
        self.assertEqual(s["range"], ["192.168.1.0/24", "192.168.2.0/24"])
        self.assertEqual(s["ports"], ["554", "8554"])

    def test_default_scan_defaults_when_absent(self):
        orig = config._load_defaults
        config._load_defaults = lambda: {"scan": {}}
        try:
            s = config.default_scan()
        finally:
            config._load_defaults = orig
        self.assertEqual(s, {"range": [], "ports": ["554"], "logins": [], "passwords": []})

    def test_settings_default_and_update_persist(self):
        # default when unset
        self.assertEqual(config.get_settings()["save_path"], config.DEFAULT_SAVE_PATH)
        # update expands ~ to an absolute path and persists
        out = config.update_settings({"save_path": "~/shots"})
        self.assertTrue(os.path.isabs(out["save_path"]))
        self.assertTrue(out["save_path"].endswith("/shots"))
        with open(config.CONFIG_PATH) as f:
            self.assertEqual(json.load(f)["settings"]["save_path"], out["save_path"])
        # blank/absent save_path leaves the current value untouched
        config.update_settings({"save_path": ""})
        self.assertEqual(config.get_settings()["save_path"], out["save_path"])

    def test_motion_popup_setting_defaults_off_and_persists(self):
        self.assertFalse(config.get_settings()["motion_popup"])          # off by default
        out = config.update_settings({"motion_popup": True})
        self.assertTrue(out["motion_popup"])
        with open(config.CONFIG_PATH) as f:
            self.assertIs(json.load(f)["settings"]["motion_popup"], True)  # persisted as a bool
        # updating an unrelated setting leaves motion_popup intact
        config.update_settings({"save_path": "~/x"})
        self.assertTrue(config.get_settings()["motion_popup"])
        # and it can be turned back off
        self.assertFalse(config.update_settings({"motion_popup": False})["motion_popup"])

    def test_device_label_prefers_name_then_host(self):
        d = self._add(name="Front", host="9.9.9.9")
        self.assertEqual(config.device_label(d["id"]), "Front")
        d2 = self._add(name="", host="8.8.8.8")
        self.assertEqual(config.device_label(d2["id"]), "8.8.8.8")
        self.assertEqual(config.device_label("missing"), "camera")

    def test_persistence_and_reload(self):
        d = self._add(host="9.9.9.9")
        # file written, restricted to the user
        self.assertTrue(os.path.exists(config.CONFIG_PATH))
        mode = stat.S_IMODE(os.stat(config.CONFIG_PATH).st_mode)
        self.assertEqual(mode, 0o600)
        with open(config.CONFIG_PATH) as f:
            saved = json.load(f)
        self.assertEqual(saved["devices"][0]["host"], "9.9.9.9")

        # fresh load restores state (simulates a restart)
        config._state = {"devices": []}
        config.load()
        self.assertEqual(config.get_cfg(d["id"])["host"], "9.9.9.9")


if __name__ == "__main__":
    unittest.main()
