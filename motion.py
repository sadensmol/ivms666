"""Motion-detection grid codec and read/modify/write.

Hikvision packs the detection area as a hex "gridMap": columnGranularity x
rowGranularity cells (commonly 22 x 18), row-major, 8 cells per byte,
most-significant bit first. Flip GRID_MSB_FIRST if a device uses the opposite
bit order (the editor shows the decoded current area so you can verify).

Saving is read/modify/write: a fresh GET, then a byte-preserving string edit of
ONLY the <gridMap> (and optionally the layout <sensitivityLevel>), then PUT.
E-mail / notification linkage lives elsewhere (/ISAPI/Event/triggers) and is
never touched. The write is gated by a round-trip self-check.
"""

import re

from . import camera

GRID_MSB_FIRST = True

# The device's sensitivityLevel is a small 0..6 scale (NOT 0..100). Writing an
# out-of-range value returns HTTP 403 and then locks further config writes for a
# while — always clamp before sending. Verified empirically against the DVR;
# the capabilities endpoint does not expose the range.
SENS_MIN = 0
SENS_MAX = 6


def _bytes_per_row(cols):
    """Each grid row is padded to a whole number of bytes (Hikvision layout)."""
    return (cols + 7) // 8


def _bitpos(c):
    return (7 - (c % 8)) if GRID_MSB_FIRST else (c % 8)


def decode_gridmap(hexstr, cols, rows):
    """Decode a Hikvision gridMap into a rows x cols boolean grid.

    Each row of `cols` cells is byte-aligned: ceil(cols/8) bytes per row,
    MSB-first. So a 22x18 grid is 3 bytes/row * 18 = 54 bytes = 108 hex chars
    (22 real cells + 2 padding bits per row).
    """
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        data = b""
    bpr = _bytes_per_row(cols)
    grid = [[False] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            bi = r * bpr + (c // 8)
            if bi < len(data):
                grid[r][c] = bool((data[bi] >> _bitpos(c)) & 1)
    return grid


def apply_cells(hexstr, cells, cols, rows):
    """Return a gridMap of the SAME length as `hexstr`, flipping only the bits
    for the real columns and leaving every other bit (row padding included)
    exactly as the device had it.

    This is what makes a save byte-preserving and safe: even if our
    cols/rows/bit-order guess were wrong, only the cells we intend to change are
    touched — unrelated bits (and the device's padding bits) are never disturbed.
    """
    data = bytearray.fromhex(hexstr)
    bpr = _bytes_per_row(cols)
    for r in range(rows):
        for c in range(cols):
            bi = r * bpr + (c // 8)
            if bi >= len(data):
                continue
            mask = 1 << _bitpos(c)
            if cells[r][c]:
                data[bi] |= mask
            else:
                data[bi] &= (~mask) & 0xFF
    return data.hex()


def encode_gridmap(grid, hexlen, cols, rows):
    """Build a gridMap from scratch (row-aligned, padding bits zero). Handy for
    tests/tools; a real save uses apply_cells so the device's padding survives."""
    data = bytearray(hexlen // 2)
    bpr = _bytes_per_row(cols)
    for r in range(rows):
        for c in range(cols):
            if grid[r][c]:
                bi = r * bpr + (c // 8)
                if bi < len(data):
                    data[bi] |= 1 << _bitpos(c)
    return data.hex()


def _re_first(pattern, text, default=None):
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else default


def get_motion(cfg, input_id):
    """Read the current motion area/sensitivity for a video input."""
    _, raw = camera.camera_get(
        cfg, f"/ISAPI/System/Video/inputs/channels/{input_id}/motionDetection")
    text = raw.decode("utf-8", "replace")
    gridmap = _re_first(r"<gridMap>([0-9a-fA-F]*)</gridMap>", text)
    cols = int(_re_first(r"<columnGranularity>(\d+)</columnGranularity>", text, "22"))
    rows = int(_re_first(r"<rowGranularity>(\d+)</rowGranularity>", text, "18"))
    enabled = (_re_first(r"<enabled>(\w+)</enabled>", text, "false") == "true")
    sensitivity = int(_re_first(r"<sensitivityLevel>(\d+)</sensitivityLevel>", text, "50"))
    if gridmap is None:
        return {"format": "unsupported", "enabled": enabled, "cols": cols, "rows": rows,
                "sensitivity": sensitivity, "cells": []}
    grid = decode_gridmap(gridmap, cols, rows)
    return {"format": "grid", "enabled": enabled, "cols": cols, "rows": rows,
            "sensitivity": sensitivity,
            "cells": [[1 if v else 0 for v in row] for row in grid]}


def _replace_layout_sensitivity(text, value):
    value = str(int(value))
    lay = re.search(r"(<MotionDetectionLayout>.*?</MotionDetectionLayout>)", text, re.S)
    if lay:
        new_block, n = re.subn(r"(<sensitivityLevel>)\d+(</sensitivityLevel>)",
                               r"\g<1>" + value + r"\g<2>", lay.group(1), count=1)
        if n:
            return text[:lay.start(1)] + new_block + text[lay.end(1):]
    new_text, n = re.subn(r"(<sensitivityLevel>)\d+(</sensitivityLevel>)",
                          r"\g<1>" + value + r"\g<2>", text, count=1)
    return new_text if n else text


def set_motion(cfg, input_id, cells, sensitivity):
    """Update ONLY the motion grid (+ optional sensitivity), preserving all else."""
    endpoint = f"/ISAPI/System/Video/inputs/channels/{input_id}/motionDetection"
    _, raw = camera.camera_get(cfg, endpoint)  # fresh read, right before write
    text = raw.decode("utf-8", "replace")

    m = re.search(r"<gridMap>([0-9a-fA-F]*)</gridMap>", text)
    if not m:
        raise ValueError("this camera does not use the grid motion format; not modifying it")
    cur = m.group(1)
    cols = int(_re_first(r"<columnGranularity>(\d+)</columnGranularity>", text, "22"))
    rows = int(_re_first(r"<rowGranularity>(\d+)</rowGranularity>", text, "18"))

    # Safety self-check: a byte-preserving re-apply of the device's current area
    # must reproduce its own gridMap exactly (catches odd-length / invalid hex),
    # otherwise we don't understand the format and must not write.
    try:
        roundtrip = apply_cells(cur, decode_gridmap(cur, cols, rows), cols, rows)
    except ValueError:
        roundtrip = None
    if roundtrip != cur.lower():
        raise ValueError("unrecognized grid encoding for this device; refusing to write")
    if len(cells) != rows or any(len(row) != cols for row in cells):
        raise ValueError(f"grid must be {rows} rows x {cols} cols")

    new_map = apply_cells(cur, cells, cols, rows)  # same length; preserves padding bits
    new_text = text[:m.start(1)] + new_map + text[m.end(1):]  # equal length -> offsets valid
    if sensitivity is not None:
        clamped = max(SENS_MIN, min(SENS_MAX, int(sensitivity)))
        new_text = _replace_layout_sensitivity(new_text, clamped)

    _, resp = camera.camera_put(cfg, endpoint, new_text.encode("utf-8"))
    body = resp.decode("utf-8", "replace")
    ok = ("<statusString>OK" in body) or ("statusCode>1<" in body) or body.strip() == ""
    return ok, body[:400]
