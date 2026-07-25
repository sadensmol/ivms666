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
