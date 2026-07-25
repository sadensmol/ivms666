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
