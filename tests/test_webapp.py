"""Tests for the web API. Skipped unless the [web] extra (+httpx) is installed."""

import unittest

try:
    import httpx  # noqa: F401  (required by fastapi TestClient)
    from fastapi.testclient import TestClient

    from imap_cleanup_tool.webapp import create_app
    _HAVE_WEB = True
except Exception:  # pragma: no cover - depends on optional deps
    _HAVE_WEB = False


@unittest.skipUnless(_HAVE_WEB, "web extra (fastapi + httpx) not installed")
class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app())

    def test_meta(self):
        data = self.client.get("/api/meta").json()
        self.assertIn("Gmail", data["providers"])
        self.assertIn("date", data["operators"])

    def test_index_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("IMAP Cleanup Tool", r.text)

    def test_validate_rule_ok(self):
        tree = {"type": "condition", "field": "sender",
                "operator": "contains", "value": "amazon.com"}
        r = self.client.post("/api/validate-rule", json={"tree": tree})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["search"], 'FROM "amazon.com"')

    def test_validate_rule_rejects_empty_group(self):
        r = self.client.post("/api/validate-rule",
                             json={"tree": {"type": "group", "op": "AND",
                                            "children": []}})
        self.assertEqual(r.status_code, 400)

    def test_run_rejects_empty_targets(self):
        r = self.client.post("/api/run", json={
            "conn": {"host": "h", "user": "u"},
            "match_mode": "targets", "targets_text": "   ", "dry_run": True})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
