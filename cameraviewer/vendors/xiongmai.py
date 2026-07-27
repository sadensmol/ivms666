"""XiongMai (XM) — the chipset/firmware behind a huge number of cheap white-label
Chinese DVRs/cameras (sold under hundreds of brand names).

Its 401 digest realm is `Login to <32-hex hash>` (an MD5-ish device id), which
`vendors.detect` distinguishes from a Dahua-style serial. Channels are **0-based**
in the `/live/chNN_S` scheme (`{c0}` = channel-1), stream `0` main / `1` sub; many
XM boxes also accept the Dahua `/cam/realmonitor` path, so it's included as a
fallback syntax.

Note: XM's other well-known path embeds credentials in the query
(`/user=<u>&password=<p>&channel=1&stream=0.sdp?real_stream`); that form doesn't
fit digest-auth verification, so it isn't probed — the digest-compatible paths
below are.
"""

from .base import Vendor

XIONGMAI = Vendor(
    name="XiongMai",
    realm_keywords=(),      # matched by the hash-realm regex in vendors.detect, not a keyword
    channel_streams=(
        ("/live/ch{c0:02d}_0", "/av{c0}_0",
         "/cam/realmonitor?channel={c}&subtype=0"),     # main
        ("/live/ch{c0:02d}_1", "/av{c0}_1",
         "/cam/realmonitor?channel={c}&subtype=1"),     # sub
    ),
)
