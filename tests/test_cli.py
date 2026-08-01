"""cli._LiveRegion: the flood guards. The live block must never emit a line
wider than the terminal (would wrap onto a 2nd physical row) nor more lines than
the terminal is tall (would make render's cursor-up clamp and leak to scrollback
— the original 'tons of trying … lines' flood)."""

import os
import unittest
from unittest import mock

from cameraviewer import cli


def _fit(lines, cols, rows):
    with mock.patch.object(cli.shutil, "get_terminal_size",
                           return_value=os.terminal_size((cols, rows))):
        return cli._LiveRegion()._fit(lines)


class LiveRegionFitTest(unittest.TestCase):
    def test_truncates_each_line_to_width(self):
        # A long "trying …" line is clipped to cols-1 so it can't wrap.
        [line] = _fit(["x" * 500], cols=40, rows=50)
        self.assertEqual(len(line), 39)

    def test_caps_block_to_height_and_marks_overflow(self):
        # More verifying hosts than the terminal is tall -> capped, surplus
        # collapsed into a single '+N more' line (never taller than rows-1).
        out = _fit([f"host{i}" for i in range(30)], cols=200, rows=10)
        self.assertEqual(len(out), 9)                 # rows - 1
        self.assertIn("more verifying", out[-1])

    def test_small_block_passes_through(self):
        out = _fit(["a", "b", "c"], cols=200, rows=50)
        self.assertEqual(out, ["a", "b", "c"])

    def test_non_tty_render_is_noop(self):
        region = cli._LiveRegion()
        region.tty = False
        region.render(["a", "b"])                     # must not touch cursor state
        self.assertEqual(region.n, 0)


if __name__ == "__main__":
    unittest.main()
