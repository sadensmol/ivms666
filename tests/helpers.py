"""Shared test helpers: a fake camera transport and ISAPI payload builders.

The fake patches `camera.camera_get` / `camera_put`, the lowest
network layer, so the *real* discovery and motion logic runs on top of it.
"""

import io

import camera, motion


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


def video_inputs_xml(count=2, disabled=()):
    """Video-input list. Ids in `disabled` are rendered as an empty slot with no
    camera (videoInputEnabled=false / resDesc=NO VIDEO)."""
    ns = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'
    off = {str(x) for x in disabled}
    rows = "".join(
        f"<VideoInputChannel><id>{i}</id><name>Camera {i:02d}</name>"
        f"<inputPort>{i}</inputPort>"
        f"<videoInputEnabled>{'false' if str(i) in off else 'true'}</videoInputEnabled>"
        f"<resDesc>{'NO VIDEO' if str(i) in off else '1080P30'}</resDesc>"
        f"</VideoInputChannel>"
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


def record_track_xml(mode="CMR", pre=5, post=5, actions=2, enable=True):
    """A recording-track payload shaped like the DVR's, for diagnose tests."""
    acts = "".join(
        f"<ScheduleAction><id>{i}</id><Actions><Record>true</Record>"
        f"<ActionRecordingMode>{mode}</ActionRecordingMode></Actions></ScheduleAction>"
        for i in range(1, actions + 1)
    )
    return (
        '<Track version="1.0" xmlns="http://www.std-cgi.com/ver20/XMLSchema"><id>101</id>'
        f"<Enable>{'true' if enable else 'false'}</Enable>"
        f"<DefaultRecordingMode>{mode}</DefaultRecordingMode>"
        f"<TrackSchedule><ScheduleBlockList><ScheduleBlock>{acts}</ScheduleBlock>"
        "</ScheduleBlockList></TrackSchedule>"
        "<CustomExtensionList><CustomExtension>"
        f"<PreRecordTimeSeconds>{pre}</PreRecordTimeSeconds>"
        f"<PostRecordTimeSeconds>{post}</PostRecordTimeSeconds>"
        "</CustomExtension></CustomExtensionList></Track>"
    ).encode()


def stream_channel_xml(width=1920, height=1080):
    """A main-stream StreamingChannel with a resolution, for quality checks."""
    return (
        '<StreamingChannel xmlns="http://www.hikvision.com/ver20/XMLSchema"><id>101</id>'
        f"<Video><videoResolutionWidth>{width}</videoResolutionWidth>"
        f"<videoResolutionHeight>{height}</videoResolutionHeight></Video></StreamingChannel>"
    ).encode()


def stream_caps_xml(max_w=1920, max_h=1080):
    """Streaming capabilities advertising the resolution options (opt lists)."""
    return (
        '<StreamingChannel xmlns="http://www.hikvision.com/ver20/XMLSchema"><Video>'
        f'<videoResolutionWidth opt="{max_w},1280,704">1920</videoResolutionWidth>'
        f'<videoResolutionHeight opt="{max_h},720,576">1080</videoResolutionHeight>'
        "</Video></StreamingChannel>"
    ).encode()


def time_xml(skew_secs=0, tz="+03:00"):
    """A /ISAPI/System/time payload whose localTime is `skew_secs` away from real
    local time in `tz` (0 = correct). Uses live now() so diagnose's clock check
    sees an actual diff."""
    import datetime as _dt
    sign = 1 if tz[0] == "+" else -1
    hh, mm = tz[1:].split(":")
    off = sign * (int(hh) * 3600 + int(mm) * 60)
    local = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=off + skew_secs)
    stamp = local.strftime("%Y-%m-%dT%H:%M:%S") + tz
    return (f'<Time version="2.0"><timeMode>manual</timeMode>'
            f"<localTime>{stamp}</localTime><timeZone>CST-3:00:00</timeZone></Time>").encode()


def cmsearch_result_xml(spans):
    """A CMSearchResult with one motion match per (start_iso, end_iso) span."""
    items = "".join(
        f"<searchMatchItem><trackID>101</trackID>"
        f"<timeSpan><startTime>{s}</startTime><endTime>{e}</endTime></timeSpan>"
        f"<mediaSegmentDescriptor><playbackURI>rtsp://x/Streaming/tracks/101/?starttime={s}"
        f"</playbackURI></mediaSegmentDescriptor><metadataMatches>"
        f"<metadataDescriptor>recordType.meta.std-cgi.com/motion</metadataDescriptor>"
        f"</metadataMatches></searchMatchItem>"
        for s, e in spans
    )
    return (f'<CMSearchResult><searchID>x</searchID><responseStatus>true</responseStatus>'
            f"<numOfMatches>{len(spans)}</numOfMatches><matchList>{items}</matchList>"
            f"</CMSearchResult>").encode()


class FakeCamera:
    """Records requests and dispatches to a caller-provided handler.

    handler(method, path, body) -> (content_type, bytes); raise to simulate a
    camera error. Use as a context manager to patch/restore the camera module.
    """

    def __init__(self, handler):
        self.handler = handler
        self.gets = []
        self.puts = []
        self.posts = []
        self._orig = None

    def _get(self, cfg, path, timeout=15):
        self.gets.append(path)
        return self.handler("GET", path, None)

    def _put(self, cfg, path, body, timeout=15):
        self.puts.append((path, body))
        return self.handler("PUT", path, body)

    def _post(self, cfg, path, body, timeout=30):
        self.posts.append((path, body))
        return self.handler("POST", path, body)

    def __enter__(self):
        self._orig = (camera.camera_get, camera.camera_put, camera.camera_post)
        camera.camera_get, camera.camera_put, camera.camera_post = self._get, self._put, self._post
        return self

    def __exit__(self, *exc):
        camera.camera_get, camera.camera_put, camera.camera_post = self._orig
        return False
