"""Server-side snapshot saving: writes a max-res JPEG to the configured folder."""

import os
import tempfile
import unittest

from cameraviewer import config, store
from tests.helpers import FAKE_JPEG, FakeCamera


def camera_handler(method, path, body):
    if "/picture" in path:
        return ("image/jpeg", FAKE_JPEG)
    raise AssertionError("unexpected camera path: " + path)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_path = config.CONFIG_PATH
        config.CONFIG_PATH = os.path.join(self.tmp, "cfg.json")
        self.save_dir = os.path.join(self.tmp, "shots")
        config._state = {"devices": [], "settings": {"save_path": self.save_dir}}
        self.fake = FakeCamera(camera_handler)
        self.fake.__enter__()

    def tearDown(self):
        self.fake.__exit__()
        config.CONFIG_PATH = self._orig_path
        config._state = {"devices": []}

    def _cfg(self):
        return {"host": "1.1.1.1", "port": "80", "user": "admin", "password": "p"}

    def test_saves_file_with_max_resolution(self):
        path = store.save_snapshot(self._cfg(), "101", label="Front NVR")
        self.assertTrue(os.path.exists(path))          # folder was created + file written
        with open(path, "rb") as f:
            self.assertEqual(f.read(), FAKE_JPEG)
        self.assertTrue(os.path.dirname(path) == self.save_dir)
        # asked the device for its max still, not the low default
        self.assertTrue(any("videoResolutionWidth=1280" in p for p in self.fake.gets))

    def test_filename_sanitizes_label_and_flags_motion(self):
        path = store.save_snapshot(self._cfg(), "201", label="back/yard cam", motion=True)
        name = os.path.basename(path)
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith("_motion.jpg"))
        self.assertIn("_201_", name)

    def test_malicious_channel_id_stays_inside_folder(self):
        # a browser-supplied ch must not escape the save folder via path separators
        path = store.save_snapshot(self._cfg(), "../../etc/evil", label="cam")
        self.assertEqual(os.path.dirname(os.path.realpath(path)), os.path.realpath(self.save_dir))
        self.assertNotIn("/", os.path.basename(path))


if __name__ == "__main__":
    unittest.main()
