"""Web dashboard integration tests.

Spins up the real stdlib server on an ephemeral localhost port and drives it
with http.client, verifying authentication, member isolation (NFR-8), status
marking (FR-35), and that admin views are owner-only (IR-7).
"""

from __future__ import annotations

import http.client
import threading
import unittest
from dataclasses import replace

from jobradar.web.app import AppContext, build_server
from jobradar.db import utcnow
from tests.helpers import MEMORY_ITEMS, make_service, raw


class WebTest(unittest.TestCase):
    def setUp(self):
        MEMORY_ITEMS.clear()
        self.svc = make_service()
        # Bind to an ephemeral port.
        self.svc.config = replace(self.svc.config, web_port=0)
        self.store = self.svc.store

        self.store.upsert_source("src", "memory", "A")
        self.alice = self.store.create_user("alice", keywords=["backend"])
        self.bob = self.store.create_user("bob", keywords=["backend"])
        self.owner = self.store.create_user("owner", is_owner=True, keywords=["backend"])
        self.alice_token = self.store.issue_dashboard_token(self.alice)
        self.bob_token = self.store.issue_dashboard_token(self.bob)
        self.owner_token = self.store.issue_dashboard_token(self.owner)

        MEMORY_ITEMS["src"] = [raw("src", "1", "Hiring backend engineer remote worldwide")]
        self.svc.scheduler.run_once("manual")

        ctx = AppContext(store=self.store, config=self.svc.config, trigger_run=lambda: None)
        self.server = build_server(ctx)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.svc.close()

    def _req(self, method, path, token=None, body=None):
        conn = http.client.HTTPConnection(self.host, self.port)
        headers = {}
        if token:
            headers["Cookie"] = f"sid={token}"
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, data, resp.getheader("Location")

    def test_requires_login(self):
        status, _, loc = self._req("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(loc, "/login")

    def test_login_and_see_own_postings(self):
        status, data, _ = self._req("GET", "/", token=self.alice_token)
        self.assertEqual(status, 200)
        self.assertIn("backend", data.lower())

    def test_member_isolation(self):
        # A posting id belonging to Alice must not be actionable as Bob, and the
        # dashboard shows each user only their own rows (NFR-8). Here both match,
        # but the review/status routes are scoped by session user.
        pid = self.store.pending_delivery(self.alice)[0]["id"]
        # Bob dismisses using Alice's posting id → no effect on Alice.
        self._req("POST", "/status", token=self.bob_token,
                  body=f"posting_id={pid}&action=dismissed")
        # Alice still sees it as not dismissed.
        remaining = [p["id"] for p in self.store.list_user_postings(self.alice)]
        self.assertIn(pid, remaining)

    def test_status_marking_dismiss(self):
        pid = self.store.pending_delivery(self.alice)[0]["id"]
        self._req("POST", "/status", token=self.alice_token,
                  body=f"posting_id={pid}&action=dismissed")
        # FR-35: dismissed never reappears in the default listing.
        remaining = [p["id"] for p in self.store.list_user_postings(self.alice)]
        self.assertNotIn(pid, remaining)

    def test_admin_owner_only(self):
        status, _, _ = self._req("GET", "/admin", token=self.alice_token)
        self.assertEqual(status, 403)
        status, data, _ = self._req("GET", "/admin", token=self.owner_token)
        self.assertEqual(status, 200)
        self.assertIn("Run history", data)

    def test_export_is_json(self):
        status, data, _ = self._req("GET", "/export", token=self.alice_token)
        self.assertEqual(status, 200)
        self.assertIn('"user"', data)


if __name__ == "__main__":
    unittest.main()
