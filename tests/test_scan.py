"""rtsp-scan tests: IP-range expansion, credential combos, the RFC 2617 digest
codec, and end-to-end credential verification against a tiny in-process RTSP
server that answers a 401 challenge and validates the digest with raw hashlib
(so it can't be tautological with scan.py's own implementation)."""

import hashlib
import socketserver
import threading
import unittest

from cameraviewer import scan

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
                self._reply(200, "OK", cseq, body="v=0\r\n")
                return
            self._reply(401, "Unauthorized", cseq,
                        extra=f'WWW-Authenticate: Digest realm="Embedded Net DVR", '
                              f'nonce="{NONCE}", qop="auth"')
            if not auth:
                continue  # wait for the client's authenticated retry
            return

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


class FakeRTSP:
    """Context manager: a localhost RTSP server accepting `valid_creds`."""

    def __init__(self, valid_creds):
        self.valid_creds = valid_creds

    def __enter__(self):
        self.srv = _Server(("127.0.0.1", 0), _RTSPHandler)
        self.srv.valid_creds = self.valid_creds
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
    def test_shapes_import_ready_dict(self):
        e = scan.device_entry("1.2.3.4", "554", "admin", "p")
        self.assertEqual(e, {"name": "cam 1.2.3.4", "host": "1.2.3.4",
                             "port": "80", "user": "admin", "password": "p",
                             "rtsp_port": "554"})

    def test_http_port_defaults_to_80_and_is_overridable(self):
        self.assertEqual(scan.device_entry("h", 554, "u", "p")["port"], "80")
        self.assertEqual(scan.device_entry("h", 554, "u", "p", http_port="8000")["port"], "8000")


class TestScanAndVerify(unittest.TestCase):
    def test_detects_and_verifies_each_rtsp_port(self):
        with FakeRTSP({"admin": "letmein"}) as a, FakeRTSP({"admin": "letmein"}) as b:
            ports = [str(a.port), str(b.port), "1"]  # a,b speak RTSP; port 1 is dead
            combos = scan.credential_combos(["admin"], ["letmein"])
            hits = scan.scan_and_verify(["127.0.0.1"], ports, combos, timeout=3, workers=10)
        got = {(h["port"], tuple((u, p) for u, p, _ in h["working"])) for h in hits}
        self.assertEqual(got, {(str(a.port), (("admin", "letmein"),)),
                               (str(b.port), (("admin", "letmein"),))})

    def test_parallel_across_hosts_reports_progress_per_host(self):
        seen = []
        with FakeRTSP({"admin": "x"}) as srv:
            hosts = ["127.0.0.1", "127.0.0.1", "127.0.0.1"]  # 3 host slots -> 3 workers
            hits = scan.scan_and_verify(hosts, [str(srv.port)],
                                        scan.credential_combos(["admin"], ["x"]),
                                        timeout=3, workers=10,
                                        on_host_done=lambda d, t, h: seen.append((d, t)))
        self.assertEqual(len(hits), 3)                       # one hit per host slot
        self.assertEqual(sorted(seen), [(1, 3), (2, 3), (3, 3)])

    def test_no_creds_still_reports_rtsp_hits(self):
        with FakeRTSP({"admin": "x"}) as srv:
            hits = scan.scan_and_verify(["127.0.0.1"], [str(srv.port)], [], timeout=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["working"], [])


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


class TestProbeCredentials(unittest.TestCase):
    def test_finds_only_working_creds(self):
        with FakeRTSP({"admin": "letmein"}) as srv:
            combos = scan.credential_combos(["admin", "root"], ["letmein", "wrong"])
            working = scan.probe_credentials(srv.host, srv.port, combos, timeout=3)
        self.assertEqual([(u, p) for u, p, _ in working], [("admin", "letmein")])

    def test_stops_at_first_working_credential(self):
        with FakeRTSP({"admin": "letmein", "root": "toor"}) as srv:
            combos = [("admin", "letmein"), ("root", "toor")]  # both valid
            working = scan.probe_credentials(srv.host, srv.port, combos, timeout=3)
        self.assertEqual([(u, p) for u, p, _ in working], [("admin", "letmein")])

    def test_collects_all_when_stop_disabled(self):
        with FakeRTSP({"admin": "letmein", "root": "toor"}) as srv:
            combos = [("admin", "letmein"), ("root", "toor")]
            working = scan.probe_credentials(srv.host, srv.port, combos,
                                             timeout=3, stop_on_first=False)
        self.assertEqual([(u, p) for u, p, _ in working],
                         [("admin", "letmein"), ("root", "toor")])

    def test_on_attempt_called_per_try_until_stop(self):
        seen = []
        with FakeRTSP({"admin": "letmein"}) as srv:
            combos = [("admin", "wrong"), ("admin", "letmein"), ("root", "x")]
            scan.probe_credentials(srv.host, srv.port, combos, timeout=3,
                                   on_attempt=lambda i, n, u, p: seen.append((i, n, u, p)))
        # stops after the 2nd (working) combo -> the 3rd is never attempted
        self.assertEqual(seen, [(1, 3, "admin", "wrong"), (2, 3, "admin", "letmein")])

    def test_no_working_creds(self):
        with FakeRTSP({"admin": "letmein"}) as srv:
            combos = scan.credential_combos(["admin"], ["nope"])
            self.assertEqual(scan.probe_credentials(srv.host, srv.port, combos, timeout=3), [])

    def test_empty_creds_short_circuits(self):
        self.assertEqual(scan.probe_credentials("127.0.0.1", 1, []), [])

    def test_describe_reports_socket_error(self):
        # Nothing listening on this port -> connection refused, code None.
        code, detail = scan.rtsp_describe("127.0.0.1", 1, "admin", "x", timeout=1)
        self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
