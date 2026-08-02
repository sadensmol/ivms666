"""rtsp-scan tests: IP-range expansion, credential combos, the RFC 2617 digest
codec, and end-to-end credential verification against a tiny in-process RTSP
server that answers a 401 challenge and validates the digest with raw hashlib
(so it can't be tautological with scan.py's own implementation)."""

import hashlib
import re
import socketserver
import threading
import time
import unittest

import scan, vendors

NONCE = "deadbeefcafe0001"


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class _RTSPHandler(socketserver.StreamRequestHandler):
    """Minimal RTSP DESCRIBE responder: unauthenticated -> 401 Digest challenge;
    authenticated -> 200 only if the digest matches server.valid_creds."""

    def handle(self):
        while True:
            req = self._read()
            if req is None:
                return
            cseq = next((l.split(":", 1)[1].strip() for l in req
                         if l.lower().startswith("cseq:")), "0")
            auth = next((l.split(":", 1)[1].strip() for l in req
                         if l.lower().startswith("authorization:")), None)
            if auth and self._digest_ok(req[0], auth):
                # auth ok -> 200 for a valid path, 404 for an unknown one (so path
                # enumeration is testable). valid_paths=None means accept any path.
                valid = getattr(self.server, "valid_paths", None)
                if valid is None or self._path(req[0]) in valid:
                    self._reply(200, "OK", cseq, body="v=0\r\n")
                else:
                    self._reply(404, "Not Found", cseq)
                return
            self._reply(401, "Unauthorized", cseq,
                        extra=f'WWW-Authenticate: Digest realm="{self.server.realm}", '
                              f'nonce="{NONCE}", qop="auth"')
            if not auth:
                continue  # wait for the client's authenticated retry
            return

    @staticmethod
    def _path(request_line):
        m = re.search(r"rtsp://[^/]+(/[^ ]*)", request_line)  # "DESCRIBE rtsp://h:p/PATH RTSP/1.0"
        return m.group(1) if m else "/"

    def _read(self):
        lines = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return None
            if raw in (b"\r\n", b"\n"):
                return lines
            lines.append(raw.decode("latin-1").rstrip("\r\n"))

    def _digest_ok(self, request_line, auth):
        if not auth.lower().startswith("digest"):
            return False
        p = dict(scan._parse_challenge(auth)[1])
        user, password = p.get("username", ""), self.server.valid_creds.get(p.get("username"))
        if password is None:
            return False
        ha1 = _md5(f"{user}:{p.get('realm','')}:{password}")
        ha2 = _md5(f"DESCRIBE:{p.get('uri','')}")
        if p.get("qop"):
            expect = _md5(f"{ha1}:{p['nonce']}:{p['nc']}:{p['cnonce']}:{p['qop']}:{ha2}")
        else:
            expect = _md5(f"{ha1}:{p['nonce']}:{ha2}")
        return p.get("response") == expect

    def _reply(self, code, text, cseq, extra="", body=""):
        head = f"RTSP/1.0 {code} {text}\r\nCSeq: {cseq}\r\n"
        if extra:
            head += extra + "\r\n"
        self.wfile.write((head + "\r\n" + body).encode())


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    valid_creds = {}
    valid_paths = None
    realm = "Embedded Net DVR"


class FakeRTSP:
    """Context manager: a localhost RTSP server accepting `valid_creds`. If
    `valid_paths` is given, only those DESCRIBE paths return 200 (others 404), so
    path/channel enumeration can be exercised. `realm` sets the 401 digest realm."""

    def __init__(self, valid_creds, valid_paths=None, realm="Embedded Net DVR"):
        self.valid_creds = valid_creds
        self.valid_paths = valid_paths
        self.realm = realm

    def __enter__(self):
        self.srv = _Server(("127.0.0.1", 0), _RTSPHandler)
        self.srv.valid_creds = self.valid_creds
        self.srv.valid_paths = self.valid_paths
        self.srv.realm = self.realm
        self.host, self.port = self.srv.server_address
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        return False


class TestExpandRange(unittest.TestCase):
    def test_single_ip(self):
        self.assertEqual(scan.expand_range("10.0.0.5"), ["10.0.0.5"])

    def test_cidr_covers_whole_block_from_dot_zero(self):
        # host part 0 -> start at .0, iterate the whole /30 (network + broadcast included)
        self.assertEqual(scan.expand_range("192.168.1.0/30"),
                         ["192.168.1.0", "192.168.1.1", "192.168.1.2", "192.168.1.3"])

    def test_cidr_starts_at_written_host_and_goes_up(self):
        hosts = scan.expand_range("192.0.0.62/24")
        self.assertEqual(hosts[0], "192.0.0.62")          # starts exactly here
        self.assertEqual(hosts[-1], "192.0.0.255")        # up to the top of the block
        self.assertEqual(len(hosts), 194)                  # 62..255 inclusive
        self.assertNotIn("192.0.0.61", hosts)             # never below the start

    def test_cidr_32_is_single_ip(self):
        self.assertEqual(scan.expand_range("85.174.140.62/32"), ["85.174.140.62"])

    def test_dash_last_octet(self):
        self.assertEqual(scan.expand_range("10.0.0.5-7"),
                         ["10.0.0.5", "10.0.0.6", "10.0.0.7"])

    def test_dash_rolls_over_octet(self):
        hosts = scan.expand_range("10.0.0.254-10.0.1.1")
        self.assertEqual(hosts, ["10.0.0.254", "10.0.0.255", "10.0.1.0", "10.0.1.1"])

    def test_reversed_range_raises(self):
        with self.assertRaises(ValueError):
            scan.expand_range("10.0.0.9-5")


class TestExpandRanges(unittest.TestCase):
    def test_multiple_specs_concatenate(self):
        hosts = scan.expand_ranges(["192.168.1.1-2", "192.168.2.1-2"])
        self.assertEqual(hosts, ["192.168.1.1", "192.168.1.2",
                                 "192.168.2.1", "192.168.2.2"])

    def test_dedupes_across_overlapping_specs(self):
        hosts = scan.expand_ranges(["10.0.0.1-3", "10.0.0.2-4"])
        self.assertEqual(hosts, ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"])

    def test_propagates_bad_spec(self):
        with self.assertRaises(ValueError):
            scan.expand_ranges(["10.0.0.0/24", "10.0.0.9-5"])


class TestCredentialCombos(unittest.TestCase):
    def test_cartesian_product(self):
        self.assertEqual(
            scan.credential_combos(["admin", "root"], ["a", "b"]),
            [("admin", "a"), ("admin", "b"), ("root", "a"), ("root", "b")])

    def test_blanks_dropped(self):
        self.assertEqual(scan.credential_combos(["admin", ""], ["", "x"]),
                         [("admin", "x")])

    def test_empty_side_yields_nothing(self):
        self.assertEqual(scan.credential_combos(["admin"], []), [])


class TestDeviceEntry(unittest.TestCase):
    def test_builds_rtsp_url_entry(self):
        e = scan.device_entry("1.2.3.4", "554", "admin", "p")
        self.assertEqual(e, {"rtsp_url": "rtsp://admin:p@1.2.3.4:554/Streaming/Channels/101"})

    def test_anonymous_url_when_no_creds(self):
        self.assertEqual(scan.device_entry("h", 8554)["rtsp_url"],
                         "rtsp://h:8554/Streaming/Channels/101")

    def test_uses_the_given_path(self):
        e = scan.device_entry("h", "554", "admin", "p", "/cam/realmonitor?channel=2&subtype=0")
        self.assertEqual(e["rtsp_url"], "rtsp://admin:p@h:554/cam/realmonitor?channel=2&subtype=0")


_HIK1 = {"/Streaming/Channels/101"}   # a 1-camera Hikvision device


class TestScanAndVerify(unittest.TestCase):
    def test_detects_and_enumerates_each_rtsp_port(self):
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as a, \
                FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as b:
            ports = [str(a.port), str(b.port), "1"]  # a,b speak RTSP; port 1 is dead
            combos = scan.credential_combos(["admin"], ["letmein"])
            hits = scan.scan_and_verify(["127.0.0.1"], ports, combos, timeout=3, workers=10)
        by_port = {h["port"]: h["streams"] for h in hits}
        for port in (str(a.port), str(b.port)):
            self.assertEqual(by_port[port],
                             [("admin", "letmein", "/Streaming/Channels/101", "Hikvision")])
        self.assertNotIn("1", by_port)                       # dead port -> no hit

    def test_slow_verification_does_not_block_host_progress(self):
        # a found port whose credential/stream probing hangs must NOT freeze the scan:
        # host progress should reach `total` while verification is still running.
        gate = threading.Event()
        orig = scan.enumerate_streams

        def _blocking_enum(*a, **k):                       # block until released
            gate.wait(5)
            return {"streams": [], "credential": None, "attempts": 0, "total": 1, "reason": "no_login"}
        scan.enumerate_streams = _blocking_enum
        reached_total = threading.Event()
        result = {}
        try:
            with FakeRTSP({"admin": "x"}, valid_paths=_HIK1) as srv:
                hosts = ["127.0.0.1", "127.0.0.1", "127.0.0.1"]   # each finds the (blocking) port
                th = threading.Thread(target=lambda: result.update(
                    hits=scan.scan_and_verify(hosts, [str(srv.port)], [("admin", "x")],
                                              timeout=3, workers=3,
                                              on_host_done=lambda d, t, h: d == t and reached_total.set())))
                th.start()
                # progress reaches all hosts even though the 3 verifications are still blocked
                self.assertTrue(reached_total.wait(3), "host progress stalled behind verification")
                gate.set()                                        # let verifications finish
                th.join(5)
            self.assertEqual(len(result["hits"]), 3)
        finally:
            gate.set()
            scan.enumerate_streams = orig

    def test_a_stuck_verification_does_not_stall_probing_of_other_ports(self):
        # Regression: one slow verification holds a slot; probing the REMAINING
        # ports/slots must keep going and progress must still reach `total`. (The
        # bug: probes held their slot through an ordered collection, so a single
        # stuck verify deadlocked the next port's acquire-loop even with free slots.)
        gate = threading.Event()
        calls = [0]
        clock = threading.Lock()
        orig = scan.enumerate_streams

        def _enum(*a, **k):                         # only the FIRST verify blocks
            with clock:
                first = calls[0] == 0
                calls[0] += 1
            if first:
                gate.wait(5)
            return {"streams": [], "credential": None, "attempts": 0, "total": 1, "reason": "no_login"}
        scan.enumerate_streams = _enum
        reached_total = threading.Event()
        try:
            with FakeRTSP({"admin": "x"}) as a, FakeRTSP({"admin": "x"}) as b:
                hosts = ["127.0.0.1"] * 3            # win=3; both ports found on every slot
                ports = [str(a.port), str(b.port)]
                th = threading.Thread(target=lambda: scan.scan_and_verify(
                    hosts, ports, [("admin", "x")], timeout=3, workers=3,
                    on_host_done=lambda d, t, h: d == t and reached_total.set()))
                th.start()
                # 1 slot is stuck on the blocked verify, but 2 remain -> probing of the
                # 2nd port must complete and progress must reach 3/3 within the timeout.
                self.assertTrue(reached_total.wait(3), "probing stalled behind one stuck verification")
                gate.set()
                th.join(5)
        finally:
            gate.set()
            scan.enumerate_streams = orig

    def test_reports_progress_per_host_slot(self):
        seen = []
        with FakeRTSP({"admin": "x"}, valid_paths=_HIK1) as srv:
            hosts = ["127.0.0.1", "127.0.0.1", "127.0.0.1"]  # 3 host slots
            hits = scan.scan_and_verify(hosts, [str(srv.port)],
                                        scan.credential_combos(["admin"], ["x"]),
                                        timeout=3, workers=10,
                                        on_host_done=lambda d, t, h: seen.append((d, t)))
        self.assertEqual(len(hits), 3)                       # one hit per host slot
        self.assertEqual(sorted(seen), [(1, 3), (2, 3), (3, 3)])

    def test_window_probes_each_port_across_all_ips_before_next_port(self):
        # port-major within a window: every IP is probed on port A before any moves
        # to port B (the requested "10 IPs on port 1, then the same 10 on port 2").
        order = []
        with FakeRTSP({"admin": "x"}) as a, FakeRTSP({"admin": "x"}) as b:
            hosts = ["127.0.0.1", "127.0.0.1"]           # two IP slots in one window
            ports = [str(a.port), str(b.port)]
            scan.scan_and_verify(hosts, ports, [], workers=10,
                                 on_found=lambda h, p, d: order.append(p))
        self.assertEqual(order, [str(a.port), str(a.port), str(b.port), str(b.port)])

    def test_no_creds_still_reports_rtsp_hits(self):
        with FakeRTSP({"admin": "x"}, valid_paths=_HIK1) as srv:
            hits = scan.scan_and_verify(["127.0.0.1"], [str(srv.port)], [], timeout=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["streams"], [])             # no creds -> not enumerated

    def test_live_callbacks_fire_found_attempt_then_verified(self):
        events = []
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as srv:
            scan.scan_and_verify(
                ["127.0.0.1"], [str(srv.port), "1"],  # port 1 is dead -> no callbacks
                scan.credential_combos(["admin"], ["wrong", "letmein"]), timeout=3,
                on_found=lambda h, p, d: events.append(("found", p)),
                on_attempt=lambda h, p, i, n, u, pw: events.append(("attempt", p, i, n, u, pw)),
                on_verified=lambda hit: events.append(("verified", hit["port"], bool(hit["streams"]))))
        # found first, then a per-credential attempt for each tried combo (in order,
        # stopping at the working one), then verified last — all for the live port
        self.assertEqual(events, [
            ("found", str(srv.port)),
            ("attempt", str(srv.port), 1, 2, "admin", "wrong"),
            ("attempt", str(srv.port), 2, 2, "admin", "letmein"),
            ("verified", str(srv.port), True),
        ])


class TestDigest(unittest.TestCase):
    def test_no_qop_matches_rfc2617(self):
        params = {"realm": "R", "nonce": "N"}
        auth = scan._digest_authorization("u", "p", "DESCRIBE", "rtsp://h/s", params)
        ha1 = _md5("u:R:p")
        ha2 = _md5("DESCRIBE:rtsp://h/s")
        self.assertIn(f'response="{_md5(f"{ha1}:N:{ha2}")}"', auth)
        self.assertNotIn("qop", auth)

    def test_qop_auth_uses_cnonce_and_nc(self):
        params = {"realm": "R", "nonce": "N", "qop": "auth"}
        auth = scan._digest_authorization("u", "p", "DESCRIBE", "rtsp://h/s",
                                          params, cnonce="abc")
        ha1, ha2 = _md5("u:R:p"), _md5("DESCRIBE:rtsp://h/s")
        expect = _md5(f"{ha1}:N:00000001:abc:auth:{ha2}")
        self.assertIn(f'response="{expect}"', auth)
        self.assertIn("qop=auth", auth)      # bare, per RFC
        self.assertIn("nc=00000001", auth)


class TestFindCredential(unittest.TestCase):
    def test_finds_the_working_credential(self):
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as srv:
            combos = scan.credential_combos(["admin", "root"], ["wrong", "letmein"])
            got = scan.find_credential(srv.host, srv.port, combos, "/Streaming/Channels/101", timeout=3)
        self.assertEqual(got, ("admin", "letmein"))

    def test_accepts_cred_even_when_probe_path_is_wrong(self):
        # cred is right but the probe path 404s -> still "accepted" (not a 401)
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as srv:
            got = scan.find_credential(srv.host, srv.port, [("admin", "letmein")],
                                       "/does/not/exist", timeout=3)
        self.assertEqual(got, ("admin", "letmein"))

    def test_stops_at_first_working(self):
        seen = []
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as srv:
            combos = [("admin", "wrong"), ("admin", "letmein"), ("root", "x")]
            scan.find_credential(srv.host, srv.port, combos, "/Streaming/Channels/101",
                                 timeout=3, on_attempt=lambda i, n, u, p: seen.append((i, n, u, p)))
        self.assertEqual(seen, [(1, 3, "admin", "wrong"), (2, 3, "admin", "letmein")])

    def test_none_when_nothing_works(self):
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as srv:
            self.assertIsNone(scan.find_credential(srv.host, srv.port,
                                                   [("admin", "nope")], "/Streaming/Channels/101", timeout=3))

    def test_none_on_connection_error(self):
        self.assertIsNone(scan.find_credential("127.0.0.1", 1, [("admin", "x")], timeout=1))


class TestProbeStreams(unittest.TestCase):
    def test_enumerates_all_channels(self):
        paths = {"/Streaming/Channels/101", "/Streaming/Channels/102",
                 "/Streaming/Channels/201"}   # ch1 main+sub, ch2 main
        with FakeRTSP({"admin": "x"}, valid_paths=paths, realm="Embedded Net DVR") as srv:
            streams = scan.probe_streams(srv.host, srv.port, [("admin", "x")],
                                         realm="Embedded Net DVR", timeout=3)
        found = {p for _, _, p, _ in streams}
        self.assertEqual(found, paths)
        self.assertTrue(all(v == "Hikvision" for *_, v in streams))

    def test_early_stops_at_first_empty_channel(self):
        # ch1 present, ch2 empty, ch3 present -> ch3 is NOT reached (stop at ch2)
        paths = {"/Streaming/Channels/101", "/Streaming/Channels/301"}
        with FakeRTSP({"admin": "x"}, valid_paths=paths, realm="Embedded Net DVR") as srv:
            streams = scan.probe_streams(srv.host, srv.port, [("admin", "x")],
                                         realm="Embedded Net DVR", timeout=3)
        self.assertEqual({p for _, _, p, _ in streams}, {"/Streaming/Channels/101"})

    def test_uses_dahua_paths_for_dahua_realm(self):
        paths = {"/cam/realmonitor?channel=1&subtype=0"}
        with FakeRTSP({"admin": "x"}, valid_paths=paths, realm="Login to 5H013E0PAAEB114") as srv:
            streams = scan.probe_streams(srv.host, srv.port, [("admin", "x")],
                                         realm="Login to 5H013E0PAAEB114", timeout=3)
        self.assertEqual([(p, v) for _, _, p, v in streams],
                         [("/cam/realmonitor?channel=1&subtype=0", "Dahua")])

    def test_falls_back_across_vendors_when_realm_unknown(self):
        paths = {"/cam/realmonitor?channel=1&subtype=0"}   # a Dahua device, generic realm
        with FakeRTSP({"admin": "x"}, valid_paths=paths, realm="Camera") as srv:
            streams = scan.probe_streams(srv.host, srv.port, [("admin", "x")], realm="Camera", timeout=3)
        self.assertEqual([p for _, _, p, _ in streams], ["/cam/realmonitor?channel=1&subtype=0"])

    def test_no_working_cred_returns_empty(self):
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as srv:
            self.assertEqual(scan.probe_streams(srv.host, srv.port, [("admin", "nope")],
                                                realm="Embedded Net DVR", timeout=3), [])

    def test_uses_xiongmai_paths_for_hash_realm(self):
        realm = "Login to 3b833826508b074742ea1fba00fc8783"
        with FakeRTSP({"admin": "x"}, valid_paths={"/live/ch00_0"}, realm=realm) as srv:
            streams = scan.probe_streams(srv.host, srv.port, [("admin", "x")], realm=realm, timeout=3)
        self.assertEqual([(p, v) for _, _, p, v in streams], [("/live/ch00_0", "XiongMai")])


class TestEnumerateStreamsReason(unittest.TestCase):
    """The diagnostic that distinguishes "bad creds" from "good creds, no path"."""

    def test_ok_reports_credential(self):
        with FakeRTSP({"admin": "x"}, valid_paths=_HIK1, realm="Embedded Net DVR") as srv:
            r = scan.enumerate_streams(srv.host, srv.port, [("admin", "x")],
                                       realm="Embedded Net DVR", timeout=3)
        self.assertEqual(r["reason"], "ok")
        self.assertEqual(r["credential"], ("admin", "x"))
        self.assertTrue(r["streams"])

    def test_no_login_tried_every_combo(self):
        with FakeRTSP({"admin": "letmein"}, valid_paths=_HIK1) as srv:
            combos = scan.credential_combos(["admin", "root"], ["a", "b"])   # 4, none valid
            r = scan.enumerate_streams(srv.host, srv.port, combos, timeout=3)
        self.assertEqual(r["reason"], "no_login")
        self.assertIsNone(r["credential"])
        self.assertEqual((r["attempts"], r["total"]), (4, 4))

    def test_no_path_when_login_ok_but_no_stream_matches(self):
        # creds accepted (404 != 401) but NO path returns 200 -> login is fine, the
        # URL schema isn't. Must NOT claim "all combos tried" — it stopped at the 1st.
        with FakeRTSP({"admin": "x"}, valid_paths=set(), realm="Embedded Net DVR") as srv:
            r = scan.enumerate_streams(srv.host, srv.port,
                                       scan.credential_combos(["admin"], ["x", "y", "z"]),
                                       realm="Embedded Net DVR", timeout=3)
        self.assertEqual(r["reason"], "no_path")
        self.assertEqual(r["credential"], ("admin", "x"))
        self.assertEqual(r["streams"], [])
        self.assertEqual(r["attempts"], 1)              # found the login on the 1st try

    def test_conn_dropped_stops_early_and_reports_the_dropped_login(self):
        # unreachable host -> socket error -> stop after the 1st attempt, not all,
        # and report WHICH login it dropped on (so a re-run can deprioritise it).
        r = scan.find_credential_ex("127.0.0.1", 1, [("a", "1"), ("b", "2")], timeout=1)
        self.assertEqual(r["reason"], "conn_dropped")
        self.assertEqual((r["attempts"], r["total"]), (1, 2))
        self.assertEqual(r["last"], ("a", "1"))         # dropped on the first combo


class TestSharedBudget(unittest.TestCase):
    def test_probe_plus_verify_never_exceed_workers(self):
        # ONE budget of `workers`: probing an IP and verifying logins draw from the
        # same pool, so their COMBINED concurrency must never exceed it.
        lock = threading.Lock()
        live = [0]
        peak = [0]

        def _track(fn):
            def wrapped(*a, **k):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                try:
                    time.sleep(0.02)
                    return fn(*a, **k)
                finally:
                    with lock:
                        live[0] -= 1
            return wrapped

        op, oe = scan.probe_rtsp, scan.enumerate_streams
        scan.probe_rtsp, scan.enumerate_streams = _track(op), _track(oe)
        try:
            with FakeRTSP({"admin": "x"}, valid_paths=_HIK1, realm="Embedded Net DVR") as srv:
                hosts = ["127.0.0.1"] * 8
                scan.scan_and_verify(hosts, [str(srv.port)], [("admin", "x")],
                                     timeout=3, workers=3)
        finally:
            scan.probe_rtsp, scan.enumerate_streams = op, oe
        self.assertLessEqual(peak[0], 3)
        self.assertGreater(peak[0], 1)                  # sanity: it really did run concurrently


class TestVendors(unittest.TestCase):
    def test_realm_detection(self):
        self.assertEqual(vendors.detect("Embedded Net DVR").name, "Hikvision")
        self.assertEqual(vendors.detect("Login to 5H013E0PAAEB114").name, "Dahua")  # serial realm
        self.assertEqual(vendors.detect("5H013E0PAAEB114").name, "Dahua")           # bare serial -> Dahua-family
        self.assertIsNone(vendors.detect("some spaced realm"))

    def test_xiongmai_hash_realm_beats_dahua(self):
        # "Login to <32-hex hash>" is XiongMai/XM, not Dahua (whose serial has non-hex letters)
        self.assertEqual(vendors.detect("Login to 3b833826508b074742ea1fba00fc8783").name, "XiongMai")

    def test_xiongmai_channels_are_zero_based(self):
        live = {"/live/ch00_0", "/live/ch01_0"}   # ch1, ch2 main (0-based path)
        codes = lambda p: 200 if p in live else 404
        self.assertEqual(vendors.XIONGMAI.streams(codes, max_channels=8),
                         ["/live/ch00_0", "/live/ch01_0"])

    def test_enumeration_order_puts_detected_first_then_generic_last(self):
        order = vendors.enumeration_order("Embedded Net DVR")
        self.assertEqual(order[0], vendors.HIKVISION)
        self.assertEqual(order[-1], vendors.GENERIC)
        self.assertEqual(len(order), len(vendors.VENDORS) + 1)   # every vendor once

    def test_panasonic_realm(self):
        self.assertEqual(vendors.detect("Panasonic Network Camera").name, "Panasonic")

    def test_generic_covers_common_oem_paths(self):
        probed = set()
        vendors.GENERIC.streams(lambda p: (probed.add(p), 404)[1], max_channels=1)
        for p in ("/live", "/media.amp", "/av0_0", "/rtsp_tunnel", "/h264.sdp",
                  "/mpeg4", "/stream", "/ch0.h264", "/gnz_media/main",
                  "/ch0_unicast_firststream"):
            self.assertIn(p, probed)

    def test_all_templates_expand_without_error(self):
        for v in vendors.VENDORS + vendors.FALLBACK:
            v.streams(lambda p: 404, max_channels=4)   # exercises every .format()
            self.assertTrue(v.probe_path())

    def test_channel_enumeration_stops_at_first_empty_channel(self):
        # describe() reports only ch1's main path as a stream; ch2 empty -> stop
        live = {"/Streaming/Channels/101"}
        codes = lambda path: 200 if path in live else 404
        got = vendors.HIKVISION.streams(codes, max_channels=8)
        self.assertEqual(got, ["/Streaming/Channels/101"])

    def test_describe_reports_socket_error(self):
        # Nothing listening on this port -> connection refused, code None.
        code, detail = scan.rtsp_describe("127.0.0.1", 1, "admin", "x", timeout=1)
        self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
