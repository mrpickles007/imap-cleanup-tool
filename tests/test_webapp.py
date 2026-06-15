"""Tests for the web API. Skipped unless the [web] extra (+httpx) is installed."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import httpx  # noqa: F401  (required by fastapi TestClient)
    from fastapi.testclient import TestClient

    from imap_cleanup_tool import llm, profiles, scheduler
    from imap_cleanup_tool.webapp import create_app
    _HAVE_WEB = True
except Exception:  # pragma: no cover - depends on optional deps
    _HAVE_WEB = False

_RULE = {"type": "condition", "field": "sender",
         "operator": "contains", "value": "amazon.com"}


@unittest.skipUnless(_HAVE_WEB, "web extra (fastapi + httpx) not installed")
class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app())

    def test_meta(self):
        data = self.client.get("/api/meta").json()
        names = [p["name"] for p in data["providers"]]
        self.assertIn("Gmail", names)
        self.assertGreater(len(names), 20)        # loaded from providers.json
        self.assertIn("date", data["operators"])

    def test_index_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("IMAP Cleanup Tool", r.text)

    def test_validate_rule_ok(self):
        r = self.client.post("/api/validate-rule", json={"tree": _RULE})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["search"], 'FROM "amazon.com"')

    def test_validate_rule_rejects_empty_group(self):
        r = self.client.post("/api/validate-rule",
                             json={"tree": {"type": "group", "op": "AND",
                                            "children": []}})
        self.assertEqual(r.status_code, 400)

    def test_run_without_session_is_rejected(self):
        r = self.client.post("/api/run", json={
            "sid": "does-not-exist", "match_mode": "rule", "rule_tree": _RULE})
        self.assertEqual(r.status_code, 440)

    def test_log_without_session_is_rejected(self):
        self.assertEqual(self.client.get("/api/log/nope").status_code, 440)

    def test_refresh_folders_without_session_is_rejected(self):
        r = self.client.post("/api/refresh-folders", json={"sid": "nope"})
        self.assertEqual(r.status_code, 440)

    def test_count_without_session_is_rejected(self):
        r = self.client.post("/api/count", json={
            "sid": "nope", "match_mode": "rule", "rule_tree": _RULE})
        self.assertEqual(r.status_code, 440)

    def test_senders_csv_without_session_is_rejected(self):
        self.assertEqual(
            self.client.get("/api/senders.csv/nope").status_code, 440)

    def test_profiles_crud(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                self.assertEqual(
                    self.client.get("/api/profiles").json()["profiles"], [])
                r = self.client.post("/api/profiles", json={
                    "name": "p", "host": "h", "user": "u", "password": "pw"})
                self.assertEqual(r.status_code, 200)
                names = [p["name"]
                         for p in self.client.get("/api/profiles").json()["profiles"]]
                self.assertIn("p", names)
                loaded = self.client.post("/api/profiles/load",
                                          json={"name": "p"}).json()
                self.assertEqual(loaded["password"], "pw")
                self.client.delete("/api/profiles/p")
                self.assertEqual(
                    self.client.get("/api/profiles").json()["profiles"], [])

    def test_job_requires_profile(self):
        r = self.client.post("/api/jobs", json={
            "name": "x", "match_mode": "rule", "rule_tree": _RULE})
        self.assertEqual(r.status_code, 400)

    def test_jobs_crud(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                # scheduled jobs connect via a non-encrypted profile
                self.client.post("/api/profiles", json={
                    "name": "prof1", "host": "imap.gmail.com",
                    "user": "u", "password": "pw"})
                save = self.client.post("/api/jobs", json={
                    "name": "t1", "profile": "prof1",
                    "match_mode": "rule", "rule_tree": _RULE,
                    "kind": "daily", "time": "03:00"})
                self.assertEqual(save.status_code, 200)
                # The OS command runs the job by name; details live in job.args.
                self.assertIn("--run-job", save.json()["command"])
                self.assertIn("t1", save.json()["command"])
                jobs = self.client.get("/api/jobs").json()["jobs"]
                t1 = next(j for j in jobs if j["name"] == "t1")
                self.assertIn("--profile", t1["args"])
                self.assertIn("prof1", t1["args"])
                self.assertIn("--rule", t1["args"])
                # the job's log can be fetched (empty until it runs)
                log = self.client.get("/api/jobs/t1/log")
                self.assertEqual(log.status_code, 200)
                self.assertEqual(log.json()["log"], "")
                self.client.delete("/api/jobs/t1")
                self.assertNotIn("t1", [j["name"]
                                        for j in self.client.get("/api/jobs").json()["jobs"]])
                # log endpoint for a missing job is a 404
                self.assertEqual(
                    self.client.get("/api/jobs/t1/log").status_code, 404)

    def test_move_job_builds_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                self.client.post("/api/profiles", json={
                    "name": "pf", "host": "imap.gmail.com",
                    "user": "u", "password": "pw"})
                r = self.client.post("/api/jobs", json={
                    "name": "mv", "profile": "pf", "match_mode": "rule",
                    "rule_tree": _RULE, "move": True, "dest_folder": "Archive",
                    "kind": "daily", "time": "03:00"})
                self.assertEqual(r.status_code, 200)
                mv = next(j for j in self.client.get("/api/jobs").json()["jobs"]
                          if j["name"] == "mv")
                self.assertIn("--move", mv["args"])
                self.assertIn("--dest-folder", mv["args"])
                self.assertIn("Archive", mv["args"])
                self.assertNotIn("--gmail-trash", mv["args"])

    def test_move_job_requires_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                self.client.post("/api/profiles", json={
                    "name": "pf", "host": "h", "user": "u", "password": "pw"})
                r = self.client.post("/api/jobs", json={
                    "name": "mv2", "profile": "pf", "match_mode": "rule",
                    "rule_tree": _RULE, "move": True, "dest_folder": "",
                    "kind": "daily"})
                self.assertEqual(r.status_code, 400)

    def test_move_all_job_builds_without_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                self.client.post("/api/profiles", json={
                    "name": "pf", "host": "h", "user": "u", "password": "pw"})
                # move + no targets + no rule => move ALL (no --targets/--rule)
                r = self.client.post("/api/jobs", json={
                    "name": "mvall", "profile": "pf", "match_mode": "targets",
                    "targets_text": "", "move": True, "dest_folder": "Archive",
                    "kind": "daily", "time": "03:00"})
                self.assertEqual(r.status_code, 200)
                j = next(x for x in self.client.get("/api/jobs").json()["jobs"]
                         if x["name"] == "mvall")
                self.assertIn("--move", j["args"])
                self.assertIn("Archive", j["args"])
                self.assertNotIn("--targets", j["args"])
                self.assertNotIn("--rule", j["args"])

    def test_create_folder_without_session_is_rejected(self):
        r = self.client.post("/api/create-folder",
                             json={"sid": "nope", "name": "X"})
        self.assertEqual(r.status_code, 440)

    def test_delete_folder_without_session_is_rejected(self):
        r = self.client.post("/api/delete-folder",
                             json={"sid": "nope", "name": "X"})
        self.assertEqual(r.status_code, 440)

    def test_ai_report_without_session_is_rejected(self):
        r = self.client.post("/api/ai-report", json={"sid": "nope"})
        self.assertEqual(r.status_code, 440)
        r2 = self.client.get("/api/ai-report.json/nope")
        self.assertEqual(r2.status_code, 440)

    def test_llm_models_crud(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                r = self.client.post("/api/llm-models", json={
                    "name": "m1", "model": "gpt-4o-mini", "api_key": "sk-x"})
                self.assertEqual(r.status_code, 200)
                models = self.client.get("/api/llm-models").json()["models"]
                self.assertTrue(any(m["name"] == "m1" for m in models))
                self.client.delete("/api/llm-models/m1")
                self.assertFalse(any(
                    m["name"] == "m1"
                    for m in self.client.get("/api/llm-models").json()["models"]))


if __name__ == "__main__":
    unittest.main()
