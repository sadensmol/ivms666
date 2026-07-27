"""A `Vendor` describes one camera/DVR family's RTSP stream paths and how to
enumerate its channels — so `scan`, after finding a port, can try the right paths
for that device and walk its cameras.

Paths are grouped by **stream**, not just listed: each group is a set of
*alternate syntaxes for the same stream* (e.g. `/Streaming/Channels/101`,
`/ISAPI/Streaming/Channels/101` and `/h264/ch1/main/av_stream` are all "channel 1
main"). Enumeration takes the **first** syntax in a group that works, so one camera
yields one URL — not a duplicate per syntax. `{c}` in a template is the 1-based
channel, expanded per channel; channel enumeration **stops at the first channel
with no stream** (so a 4-ch DVR isn't probed to the max). Everything is hardcoded
per vendor (the schemas are static).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Vendor:
    name: str
    realm_keywords: tuple = ()      # lowercase substrings of the 401 digest realm
    channel_streams: tuple = ()     # tuple of groups; each group = alt `{c}` templates for ONE stream
    single_streams: tuple = ()      # tuple of groups; each group = alt no-channel templates for ONE stream

    def matches(self, realm_lc):
        """True if this vendor's realm keyword appears in the (lowercased) realm."""
        return any(k in realm_lc for k in self.realm_keywords)

    def probe_path(self):
        """A representative path (channel 1) to test credentials against — it only
        needs to trigger the device's 401 challenge, not necessarily exist."""
        if self.channel_streams:
            return self.channel_streams[0][0].format(c=1, c0=0)
        if self.single_streams:
            return self.single_streams[0][0]
        return "/"

    def streams(self, describe, max_channels):
        """Enumerate this vendor's working stream paths.

        `describe(path)` -> RTSP status code (200 = real stream, 404 = valid
        cred/wrong path, None = connection died). For each channel, each stream
        group is tried until one syntax answers 200 (that URL is the stream);
        channels are walked 1..max_channels and **stop at the first channel with no
        stream at all**. Single (no-channel) groups are then each probed once.
        """
        found = []
        for c in range(1, max_channels + 1):
            got, dead = self._probe_groups(describe, self.channel_streams, c)
            if dead:
                return found                # connection died -> return what we have
            if not got:
                break                       # this channel has no stream -> stop climbing
            found += got
        singles, dead = self._probe_groups(describe, self.single_streams, None)
        return found + singles

    @staticmethod
    def _probe_groups(describe, groups, c):
        """One working path per group (first syntax that 200s). Returns
        (paths, connection_died). Templates may use `{c}` (1-based channel) or
        `{c0}` (0-based, e.g. XiongMai's `/live/ch{c0:02d}_0`)."""
        fmt = {"c": c, "c0": c - 1} if c is not None else {}
        out = []
        for group in groups:
            for tmpl in group:
                path = tmpl.format(**fmt)
                code = describe(path)
                if code is None:
                    return out, True
                if code == 200:
                    out.append(path)
                    break                   # same stream — don't try the other syntaxes
        return out, False
