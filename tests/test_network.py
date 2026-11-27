"""Opening the dashboard from other devices: the setting, the addresses shown,
and the QR code encoder (decoded with a real QR reader during development)."""

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from threading import Thread
from unittest import mock

from jobradar.config import Config
from jobradar.web import app as web
from jobradar.web import netinfo, qr


class QrTests(unittest.TestCase):
    def test_sizes_and_fixed_patterns(self):
        m = qr.encode("hi")
        self.assertEqual(len(m), 21)                                   # version 1
        for x, y in ((0, 0), (20, 0), (0, 20)):                         # finder corners
            self.assertTrue(m[y][x])
        self.assertEqual([m[6][x] for x in range(8, 13)], [True, False, True, False, True])   # timing
        link = "http://192.168.1.20:8765/?t=" + "A" * 43
        self.assertEqual(len(qr.encode(link)), 37)                      # version 5 at level M
        self.assertEqual(len(qr.encode("x" * 150, "L")), 45)            # version 7: version info + 3 alignments
        with self.assertRaises(qr.QrError):
            qr.encode("x" * 400)

    def test_alignment_positions_match_the_standard(self):
        self.assertEqual(qr._alignment_positions(7), [6, 22, 38])
        self.assertEqual(qr._alignment_positions(10), [6, 28, 50])

    def test_svg(self):
        s = qr.svg("hello")
        self.assertTrue(s.startswith("<svg") and s.endswith("</svg>"))
        self.assertNotIn("<script", s)


class NetinfoTests(unittest.TestCase):
    def test_addresses(self):
        with mock.patch.object(netinfo, "_primary_ipv4", return_value="192.168.1.20"), \
                mock.patch.object(netinfo.socket, "getaddrinfo",
                                  return_value=[(2, 1, 6, "", ("127.0.1.1", 0)), (2, 1, 6, "", ("100.101.1.2", 0)),
                                                (2, 1, 6, "", ("169.254.3.4", 0)), (2, 1, 6, "", ("192.168.1.20", 0))]):
            self.assertEqual(netinfo.addresses(), [{"ip": "192.168.1.20", "kind": "network"},
                                                   {"ip": "100.101.1.2", "kind": "tailscale"}])


class DashboardNetworkTests(unittest.TestCase):
    def serve(self, config_text, host=None):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        cfg = Path(d.name) / "config.toml"
        cfg.write_text(config_text, "utf-8")
        server = web.make_server(cfg, host=host, port=0)
        Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def state(self, server):
        r = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/state",
                                   headers={"Cookie": f"{web.COOKIE}={server.token}"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())["network"]

    def test_local_only_by_default(self):
        server = self.serve("")
        self.assertTrue(server.local_only)
        n = self.state(server)
        self.assertEqual((n["allow"], n["listening"], n["restart_needed"], n["addresses"]), (False, False, False, []))

    def test_switch_on_listens_and_shows_links(self):
        with mock.patch.object(netinfo, "addresses", return_value=[{"ip": "192.168.1.20", "kind": "network"}]):
            server = self.serve("web_allow_network = true\n")
            self.assertFalse(server.local_only)
            self.assertEqual(server.server_address[0], "0.0.0.0")
            n = self.state(server)
        self.assertTrue(n["listening"] and not n["restart_needed"])
        a = n["addresses"][0]
        self.assertEqual(a["url"], f"http://192.168.1.20:{server.server_address[1]}/")   # no secret: people log in
        self.assertTrue(a["qr"].startswith("<svg"))

    def test_changing_the_switch_needs_a_restart(self):
        server = self.serve("")
        with mock.patch.object(web.Controller, "config", property(lambda self: Config(web_allow_network=True))):
            self.assertTrue(self.state(server)["restart_needed"])

    def test_port_setting(self):
        import socket

        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        taken = busy.getsockname()[1]
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        cfg = Path(d.name) / "config.toml"
        cfg.write_text(f"web_port = {taken}\n", "utf-8")
        server = web.make_server(cfg)                       # no port given: the setting decides
        Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertNotEqual(server.server_address[1], taken)  # busy: the next free one
        n = self.state(server)
        self.assertEqual((n["port_setting"], n["port_busy"], n["port_restart"]), (taken, True, False))

    def test_port_validation_and_command_line(self):
        from jobradar.web import configfile as cf

        with self.assertRaises(cf.ConfigError):
            cf.coerce("web_port", 70000)
        self.assertEqual(cf.coerce("web_port", "8080"), 8080)
        server = self.serve("web_port = 8080\n")         # serve() passes port=0: the command line wins
        self.assertNotEqual(server.server_address[1], 8080)

    def test_command_line_host_wins(self):
        server = self.serve("web_allow_network = true\n", host="127.0.0.1")
        self.assertTrue(server.local_only)
        n = self.state(server)
        self.assertTrue(n["forced"])
        self.assertFalse(n["restart_needed"])


if __name__ == "__main__":
    unittest.main()
