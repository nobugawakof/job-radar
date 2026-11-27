"""Opening the dashboard from other devices: the setting, the addresses shown,
and the QR code encoder (decoded with a real QR reader during development)."""

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from threading import Thread
from unittest import mock

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

    def test_open_to_other_computers_by_default(self):
        with mock.patch.object(netinfo, "addresses", return_value=[{"ip": "192.168.1.20", "kind": "network"}]):
            server = self.serve("")
            self.assertFalse(server.local_only)
            self.assertEqual(server.server_address[0], "0.0.0.0")
            n = self.state(server)
        self.assertTrue(n["listening"])
        a = n["addresses"][0]
        self.assertEqual(a["url"], f"http://192.168.1.20:{server.server_address[1]}/")   # no secret: people log in
        self.assertTrue(a["qr"].startswith("<svg"))
        self.assertEqual(web.DEFAULT_PORT, 80)

    def test_busy_port_falls_back_to_8080(self):
        import socket

        busy = socket.socket()
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        taken = busy.getsockname()[1]
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        cfg = Path(d.name) / "config.toml"
        cfg.write_text("", "utf-8")
        server = web.make_server(cfg, port=taken)
        Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertIn(server.server_address[1], range(8080, 8090))
        self.assertTrue(self.state(server)["port_busy"])

    def test_old_settings_are_ignored(self):
        server = self.serve("web_allow_network = false\nweb_port = 8765\n")   # from older versions
        self.assertFalse(server.local_only)

    def test_command_line_host_keeps_it_local(self):
        server = self.serve("", host="127.0.0.1")
        self.assertTrue(server.local_only)
        n = self.state(server)
        self.assertEqual((n["forced"], n["listening"], n["addresses"]), (True, False, []))


class FirewallTests(unittest.TestCase):
    def run_fw(self, show_rc, show_out, add_rc):
        from jobradar.web import firewall

        calls = []

        def fake(*args):
            calls.append(args)
            rc, out = ((show_rc, show_out) if args[0] == "show" else (add_rc, "") if args[0] == "add" else (0, ""))
            return mock.Mock(returncode=rc, stdout=out)

        with mock.patch.object(firewall.sys, "platform", "win32"), mock.patch.object(firewall, "_netsh", fake):
            return firewall.ensure_open(80), calls

    def test_adds_the_rule(self):
        r, calls = self.run_fw(1, "No rules match", 0)
        self.assertEqual(r["status"], "open")
        self.assertIn("localport=80", calls[-1])

    def test_rule_already_there(self):
        r, calls = self.run_fw(0, "Rule Name: Job Radar dashboard\nLocalPort: 80\nAction: Allow", 0)
        self.assertEqual((r["status"], len(calls)), ("open", 1))

    def test_old_port_rule_is_replaced(self):
        r, calls = self.run_fw(0, "Rule Name: Job Radar dashboard\nLocalPort: 8765\nAction: Allow", 0)
        self.assertEqual([c[0] for c in calls], ["show", "delete", "add"])

    def test_without_admin_rights(self):
        r, _ = self.run_fw(1, "", 1)
        self.assertEqual(r["status"], "manual")
        self.assertIn("-LocalPort 80", r["command"])

    def test_not_windows(self):
        from jobradar.web import firewall

        with mock.patch.object(firewall.sys, "platform", "linux"):
            self.assertEqual(firewall.ensure_open(80)["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
