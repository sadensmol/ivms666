"""Shared test helpers: a fake camera transport and ISAPI payload builders.

The fake patches `cameraviewer.camera.camera_get` / `camera_put`, the lowest
network layer, so the *real* discovery and motion logic runs on top of it.
"""

import io

from cameraviewer import camera, motion


class FakeProc:
    """Stand-in for an ffmpeg subprocess.Popen used in live-stream tests."""

    def __init__(self, data=b"--ffmpeg\r\nfake-mjpeg-frame\r\n", err=b""):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(err)
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

OK_RESP = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b"<ResponseStatus><statusCode>1</statusCode>"
    b"<statusString>OK</statusString></ResponseStatus>\n"
)

# A JPEG-shaped blob (starts with the SOI marker, ends with EOI).
FAKE_JPEG = b"\xff\xd8" + b"fake-jpeg-body" + b"\xff\xd9"


def video_inputs_xml(count=2):
    ns = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'
    rows = "".join(
        f"<VideoInputChannel><id>{i}</id><name>Camera {i:02d}</name>"
        f"<inputPort>{i}</inputPort></VideoInputChannel>"
        for i in range(1, count + 1)
    )
    return f'<VideoInputChannelList {ns}>{rows}</VideoInputChannelList>'.encode()


def streaming_channels_xml(ids):
    ns = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'
    rows = "".join(
        f"<StreamingChannel><id>{i}</id><channelName>Stream {i}</channelName>"
        f"</StreamingChannel>" for i in ids
    )
    return f'<StreamingChannelList {ns}>{rows}</StreamingChannelList>'.encode()


def all_on_gridmap(cols=22, rows=18):
    """A device-shaped all-cells-on map: rows byte-aligned, padding bits set to 1
    (matches how the real DVR stores a fully-enabled grid: 22x18 -> 108 'f')."""
    return "ff" * (rows * ((cols + 7) // 8))


def motion_xml(gridmap, sensitivity=60, enabled="true", cols=22, rows=18):
    """A realistic Hikvision motionDetection payload.

    Includes surrounding settings (sampling interval, trigger times, highlight)
    so tests can assert the read/modify/write leaves them byte-for-byte intact.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MotionDetection xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">\n'
        f'  <enabled>{enabled}</enabled>\n'
        '  <enableHighlight>true</enableHighlight>\n'
        '  <samplingInterval>2</samplingInterval>\n'
        '  <startTriggerTime>500</startTriggerTime>\n'
        '  <endTriggerTime>500</endTriggerTime>\n'
        '  <regionType>grid</regionType>\n'
        '  <Grid>\n'
        f'    <rowGranularity>{rows}</rowGranularity>\n'
        f'    <columnGranularity>{cols}</columnGranularity>\n'
        '  </Grid>\n'
        '  <MotionDetectionLayout>\n'
        f'    <sensitivityLevel>{sensitivity}</sensitivityLevel>\n'
        '    <layout>\n'
        f'      <gridMap>{gridmap}</gridMap>\n'
        '    </layout>\n'
        '  </MotionDetectionLayout>\n'
        '</MotionDetection>\n'
    ).encode()


class FakeCamera:
    """Records requests and dispatches to a caller-provided handler.

    handler(method, path, body) -> (content_type, bytes); raise to simulate a
    camera error. Use as a context manager to patch/restore the camera module.
    """

    def __init__(self, handler):
        self.handler = handler
        self.gets = []
        self.puts = []
        self._orig = None

    def _get(self, cfg, path, timeout=15):
        self.gets.append(path)
        return self.handler("GET", path, None)

    def _put(self, cfg, path, body, timeout=15):
        self.puts.append((path, body))
        return self.handler("PUT", path, body)

    def __enter__(self):
        self._orig = (camera.camera_get, camera.camera_put)
        camera.camera_get, camera.camera_put = self._get, self._put
        return self

    def __exit__(self, *exc):
        camera.camera_get, camera.camera_put = self._orig
        return False
