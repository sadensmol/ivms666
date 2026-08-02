"""Motion-detection codec and read/modify/write behaviour."""

import re
import unittest

import motion
from tests.helpers import FakeCamera, OK_RESP, all_on_gridmap, motion_xml

CFG = {"host": "h", "port": "1", "user": "u", "password": "p"}
COLS, ROWS = 22, 18


class GridCodecTest(unittest.TestCase):
    def test_roundtrip_various_patterns(self):
        for seed in range(4):
            grid = [[((r * 7 + c * 3 + seed) % 2 == 0) for c in range(COLS)] for r in range(ROWS)]
            hexmap = motion.encode_gridmap(grid, 108, COLS, ROWS)
            self.assertEqual(motion.decode_gridmap(hexmap, COLS, ROWS), grid)

    def test_row_aligned_length(self):
        # 22 cols -> 3 bytes/row, 18 rows -> 54 bytes -> 108 hex chars
        hexmap = motion.encode_gridmap([[True] * COLS for _ in range(ROWS)], 108, COLS, ROWS)
        self.assertEqual(len(hexmap), 108)

    def test_apply_cells_preserves_padding_bits(self):
        # Device all-on map keeps the 2 padding bits/row set ('ff'), which a
        # from-scratch encode would drop. apply_cells must keep them.
        cur = "f" * 108
        cells = [[1] * COLS for _ in range(ROWS)]
        self.assertEqual(motion.apply_cells(cur, cells, COLS, ROWS), cur)

    def test_apply_cells_flips_only_selected_cell(self):
        cur = "f" * 108
        cells = [[1] * COLS for _ in range(ROWS)]
        cells[0][0] = 0  # clear top-left (MSB of first byte)
        out = motion.apply_cells(cur, cells, COLS, ROWS)
        self.assertTrue(out.startswith("7f"))          # 0xff -> 0x7f
        self.assertEqual(out[2:], "f" * 106)           # everything else untouched


class GetMotionTest(unittest.TestCase):
    def test_parses_grid_config(self):
        xml = motion_xml(all_on_gridmap(), sensitivity=3, enabled="true")
        with FakeCamera(lambda m, p, b: ("application/xml", xml)):
            data = motion.get_motion(CFG, "1")
        self.assertEqual(data["format"], "grid")
        self.assertEqual((data["cols"], data["rows"]), (COLS, ROWS))
        self.assertEqual(data["sensitivity"], 3)
        self.assertTrue(data["enabled"])
        self.assertEqual(sum(sum(r) for r in data["cells"]), COLS * ROWS)  # all cells on

    def test_unsupported_when_no_gridmap(self):
        xml = b"<MotionDetection><enabled>true</enabled></MotionDetection>"
        with FakeCamera(lambda m, p, b: ("application/xml", xml)):
            data = motion.get_motion(CFG, "1")
        self.assertEqual(data["format"], "unsupported")


class SetMotionTest(unittest.TestCase):
    def _run(self, xml, cells, sensitivity=3):
        captured = {}

        def handler(method, path, body):
            if method == "GET":
                return ("application/xml", xml)
            captured["body"] = body
            return ("application/xml", OK_RESP)

        with FakeCamera(handler) as fake:
            ok, msg = motion.set_motion(CFG, "1", cells, sensitivity)
        return ok, msg, captured, fake

    def test_updates_only_area_and_sensitivity(self):
        cur = all_on_gridmap()
        original = motion_xml(cur, sensitivity=3).decode()
        cells = [[1 if c < 11 else 0 for c in range(COLS)] for _ in range(ROWS)]  # left half
        ok, _, captured, fake = self._run(original.encode(), cells, sensitivity=5)

        self.assertTrue(ok)
        self.assertEqual(len(fake.puts), 1)
        new_body = captured["body"].decode()

        expected = original.replace(cur, motion.apply_cells(cur, cells, COLS, ROWS))
        expected = expected.replace("<sensitivityLevel>3</sensitivityLevel>",
                                    "<sensitivityLevel>5</sensitivityLevel>")
        self.assertEqual(new_body, expected)  # byte-for-byte except grid + sensitivity
        self.assertIn("<enableHighlight>true</enableHighlight>", new_body)
        self.assertIn("<startTriggerTime>500</startTriggerTime>", new_body)

    def test_clamps_sensitivity_to_device_range(self):
        cur = all_on_gridmap()
        cells = [[0] * COLS for _ in range(ROWS)]
        # too high -> 6
        _, _, captured, _ = self._run(motion_xml(cur, sensitivity=3), cells, sensitivity=80)
        self.assertIn("<sensitivityLevel>6</sensitivityLevel>", captured["body"].decode())
        # negative -> 0
        _, _, captured, _ = self._run(motion_xml(cur, sensitivity=3), cells, sensitivity=-4)
        self.assertIn("<sensitivityLevel>0</sensitivityLevel>", captured["body"].decode())

    def test_sensitivity_optional(self):
        cur = all_on_gridmap()
        original = motion_xml(cur, sensitivity=4).decode()
        cells = [[0] * COLS for _ in range(ROWS)]
        _, _, captured, _ = self._run(original.encode(), cells, sensitivity=None)
        self.assertIn("<sensitivityLevel>4</sensitivityLevel>", captured["body"].decode())

    def test_refuses_odd_length_gridmap(self):
        xml = motion_xml("f" * 107, sensitivity=3)  # odd length -> invalid hex bytes
        cells = [[0] * COLS for _ in range(ROWS)]
        with self.assertRaises(ValueError):
            with FakeCamera(lambda m, p, b: ("application/xml", xml)):
                motion.set_motion(CFG, "1", cells, 3)

    def test_refuses_when_no_grid_format(self):
        xml = b"<MotionDetection><enabled>true</enabled></MotionDetection>"
        cells = [[0] * COLS for _ in range(ROWS)]
        with self.assertRaises(ValueError):
            with FakeCamera(lambda m, p, b: ("application/xml", xml)):
                motion.set_motion(CFG, "1", cells, 3)

    def test_rejects_wrong_dimensions(self):
        xml = motion_xml(all_on_gridmap(), sensitivity=3)
        cells = [[0] * 10 for _ in range(10)]  # wrong shape
        with self.assertRaises(ValueError):
            with FakeCamera(lambda m, p, b: ("application/xml", xml)):
                motion.set_motion(CFG, "1", cells, 3)


if __name__ == "__main__":
    unittest.main()
