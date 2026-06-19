"""Tests for the web API. Skipped unless the [web] extra (+httpx) is installed."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import httpx  # noqa: F401  (required by fastapi TestClient)
    from fastapi.testclient import TestClient

    from imap_cleanup_tool import llm, notifications, profiles, scheduler
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
        # The Microsoft providers are flagged password-deprecated (modern auth only)
        # so the UI hides the password field and points to OAuth sign-in.
        ms = next(p for p in data["providers"]
                  if p.get("oauth_provider") == "microsoft")
        self.assertTrue(ms.get("pass_deprecated"))
        # Exactly one Microsoft IMAP preset (the two old Outlook/Hotmail rows merged).
        self.assertEqual(sum(1 for p in data["providers"]
                             if p.get("oauth_provider") == "microsoft"), 1)

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

    def test_adopt_profile_attaches_matching_profile(self):
        from imap_cleanup_tool import webapp
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                profiles.save_profile("mine", "imap.x.com", 993, "u@x.com", "pw")
                profiles.save_profile("other", "imap.y.com", 993, "v@y.com", "pw")
                sess = webapp.Session("sid-adopt", None, "imap.x.com", 993,
                                      "u@x.com")
                webapp._SESSIONS["sid-adopt"] = sess
                try:
                    # matching host+user -> adopted
                    r = self.client.post("/api/session/adopt-profile",
                                         json={"sid": "sid-adopt", "profile": "mine"})
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r.json()["profile"], "mine")
                    self.assertEqual(sess.profile, "mine")
                    # different connection -> refused (409)
                    r2 = self.client.post("/api/session/adopt-profile",
                                          json={"sid": "sid-adopt", "profile": "other"})
                    self.assertEqual(r2.status_code, 409)
                    # unknown profile -> 404
                    r3 = self.client.post("/api/session/adopt-profile",
                                          json={"sid": "sid-adopt", "profile": "nope"})
                    self.assertEqual(r3.status_code, 404)
                finally:
                    webapp._SESSIONS.pop("sid-adopt", None)

    def test_adopt_profile_without_session_is_rejected(self):
        r = self.client.post("/api/session/adopt-profile",
                             json={"sid": "nope", "profile": "x"})
        self.assertEqual(r.status_code, 440)

    def test_deleting_connected_profile_detaches_session(self):
        from imap_cleanup_tool import webapp
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                profiles.save_profile("mine", "imap.x.com", 993, "u@x.com", "pw")
                sess = webapp.Session("sid-del", None, "imap.x.com", 993,
                                      "u@x.com")
                sess.profile = "mine"               # connected via this profile
                webapp._SESSIONS["sid-del"] = sess
                try:
                    r = self.client.delete("/api/profiles/mine")
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(sess.profile, "")   # detached on delete
                finally:
                    webapp._SESSIONS.pop("sid-del", None)

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

    def test_profile_save_provider_safeguard(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                # a built-in provider host without the matching provider -> 400
                bad = self.client.post("/api/profiles", json={
                    "name": "x", "host": "imap.gmail.com", "user": "u",
                    "provider": "Custom"})
                self.assertEqual(bad.status_code, 400)
                # same host with the Gmail provider selected -> allowed
                ok = self.client.post("/api/profiles", json={
                    "name": "g", "host": "imap.gmail.com", "user": "u",
                    "provider": "Gmail"})
                self.assertEqual(ok.status_code, 200)
                # a truly custom host -> allowed
                cust = self.client.post("/api/profiles", json={
                    "name": "c", "host": "imap.mycompany.local", "user": "u"})
                self.assertEqual(cust.status_code, 200)
                self.client.delete("/api/profiles/g")
                self.client.delete("/api/profiles/c")

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
                    "user": "u", "password": "pw", "provider": "Gmail"})
                save = self.client.post("/api/jobs", json={
                    "name": "t1", "profile": "prof1",
                    "match_mode": "rule", "rule_tree": _RULE,
                    "kind": "daily", "time": "03:00"})
                self.assertEqual(save.status_code, 200)
                # The OS command runs the job by name; details live in job.args.
                self.assertIn("--run-job", save.json()["command"])
                self.assertIn("t1", save.json()["command"])
                jobs = self.client.get("/api/jobs").json()["jobs"]
                t1 = next(j for j in jobs if j["label"] == "t1")
                jid = t1["name"]                       # unique id (label + suffix)
                self.assertTrue(jid.startswith("t1_"))
                self.assertIn("--profile", t1["args"])
                self.assertIn("prof1", t1["args"])
                self.assertIn("--rule", t1["args"])
                # the job's log can be fetched (empty until it runs)
                log = self.client.get(f"/api/jobs/{jid}/log")
                self.assertEqual(log.status_code, 200)
                self.assertEqual(log.json()["log"], "")
                self.client.delete(f"/api/jobs/{jid}")
                self.assertNotIn("t1", [j["label"]
                                        for j in self.client.get("/api/jobs").json()["jobs"]])
                # log endpoint for a missing job is a 404
                self.assertEqual(
                    self.client.get(f"/api/jobs/{jid}/log").status_code, 404)

    def test_same_label_distinct_per_profile_and_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                for p in ("pa", "pb"):
                    self.client.post("/api/profiles", json={
                        "name": p, "host": "imap.gmail.com", "user": "u",
                        "password": "pw", "provider": "Gmail"})

                def body(prof):
                    return {"name": "nightly", "profile": prof,
                            "match_mode": "rule", "rule_tree": _RULE,
                            "kind": "daily", "time": "03:00"}
                a1 = self.client.post("/api/jobs", json=body("pa")).json()["saved"]
                b1 = self.client.post("/api/jobs", json=body("pb")).json()["saved"]
                # same label on two profiles -> two distinct ids, both kept
                self.assertNotEqual(a1, b1)
                jobs = self.client.get("/api/jobs").json()["jobs"]
                self.assertEqual(
                    sum(1 for j in jobs if j["label"] == "nightly"), 2)
                # re-saving the same label for the same profile UPDATES (no dup)
                a2 = self.client.post("/api/jobs", json=body("pa")).json()["saved"]
                self.assertEqual(a2, a1)
                self.assertEqual(
                    len(self.client.get("/api/jobs").json()["jobs"]), 2)

    def test_rename_job_changes_label_keeps_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                self.client.post("/api/profiles", json={
                    "name": "pf", "host": "imap.gmail.com", "user": "u",
                    "password": "pw", "provider": "Gmail"})

                def mk(nm):
                    return self.client.post("/api/jobs", json={
                        "name": nm, "profile": "pf", "match_mode": "rule",
                        "rule_tree": _RULE, "kind": "daily",
                        "time": "03:00"}).json()["saved"]
                jid = mk("old")
                r = self.client.post(f"/api/jobs/{jid}/rename",
                                     json={"label": "new"})
                self.assertEqual(r.status_code, 200)
                jobs = self.client.get("/api/jobs").json()["jobs"]
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["name"], jid)      # id unchanged
                self.assertEqual(jobs[0]["label"], "new")   # label changed
                # renaming to a label another job already uses is rejected
                jid2 = mk("other")
                clash = self.client.post(f"/api/jobs/{jid2}/rename",
                                         json={"label": "new"})
                self.assertEqual(clash.status_code, 400)

    def test_move_job_builds_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)):
                self.client.post("/api/profiles", json={
                    "name": "pf", "host": "imap.gmail.com",
                    "user": "u", "password": "pw", "provider": "Gmail"})
                r = self.client.post("/api/jobs", json={
                    "name": "mv", "profile": "pf", "match_mode": "rule",
                    "rule_tree": _RULE, "move": True, "dest_folder": "Archive",
                    "kind": "daily", "time": "03:00"})
                self.assertEqual(r.status_code, 200)
                mv = next(j for j in self.client.get("/api/jobs").json()["jobs"]
                          if j["label"] == "mv")
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
                         if x["label"] == "mvall")
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

    def test_ai_run_without_session_is_rejected(self):
        r = self.client.post("/api/ai-run", json={"sid": "nope", "model": "m"})
        self.assertEqual(r.status_code, 440)

    def test_smtp_profiles_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(notifications, "config_dir",
                                   return_value=Path(tmp)):
                self.assertEqual(
                    self.client.get("/api/smtp-profiles").json()["profiles"], [])
                r = self.client.post("/api/smtp-profiles", json={
                    "name": "ses", "host": "email-smtp.x.amazonaws.com",
                    "port": 587, "user": "AKIA", "password": "pw",
                    "from_addr": "a@b.com"})
                self.assertEqual(r.status_code, 200)
                data = self.client.get("/api/smtp-profiles").json()
                self.assertEqual(data["profiles"][0]["name"], "ses")
                s = self.client.post("/api/notify-settings", json={
                    "active": "ses", "notify_to": "me@x.com",
                    "notify_jobs": True}).json()
                self.assertEqual(s["active"], "ses")
                self.assertTrue(s["notify_jobs"])
                # missing host is a 400
                bad = self.client.post("/api/smtp-profiles", json={
                    "name": "x", "host": ""})
                self.assertEqual(bad.status_code, 400)

    def test_saved_reports_download_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                name = "ai_report_me_at_gmail.com_2026-06-15_10-00-00.csv"
                d = Path(tmp) / "ai_reports"
                d.mkdir()
                (d / name).write_text("sender,score\nx@y.com,9\n",
                                      encoding="utf-8")
                got = self.client.get("/api/ai-reports/" + name)
                self.assertEqual(got.status_code, 200)
                self.assertIn("x@y.com", got.text)
                # delete it
                self.assertEqual(self.client.delete(
                    "/api/ai-reports/" + name).status_code, 200)
                self.assertFalse((d / name).exists())
                # the per-account list needs a session -> 440 without one
                self.assertEqual(self.client.get(
                    "/api/ai-reports/list/nope").status_code, 440)
                # path traversal / bad names rejected
                self.assertEqual(self.client.get(
                    "/api/ai-reports/passwd.txt").status_code, 400)
                self.assertEqual(self.client.get(
                    "/api/ai-reports/ai_report_missing.csv").status_code, 404)

    def test_account_slug(self):
        self.assertEqual(scheduler.account_slug("Giulio@Gmail.com"),
                         "giulio_at_gmail.com")
        self.assertEqual(scheduler.account_slug(""), "unknown")

    def test_ai_job_builds_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(profiles, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(llm, "config_dir", return_value=Path(tmp)):
                self.client.post("/api/profiles", json={
                    "name": "pf", "host": "h", "user": "u", "password": "pw"})
                self.client.post("/api/llm-models", json={
                    "name": "gpt", "model": "gpt-4o-mini", "api_key": "sk-x"})
                r = self.client.post("/api/jobs", json={
                    "name": "aij", "profile": "pf", "ai_cleanup": True,
                    "ai_model": "gpt", "ai_threshold": 7, "ai_sample": 3,
                    "kind": "daily", "time": "03:00"})
                self.assertEqual(r.status_code, 200)
                j = next(x for x in self.client.get("/api/jobs").json()["jobs"]
                         if x["label"] == "aij")
                self.assertIn("--ai-cleanup", j["args"])
                self.assertIn("--ai-model", j["args"])
                self.assertIn("gpt", j["args"])
                self.assertNotIn("--targets", j["args"])

                # report-only + skip-llm: heuristic only, no model, report-only flag
                r2 = self.client.post("/api/jobs", json={
                    "name": "aij2", "profile": "pf", "ai_cleanup": True,
                    "ai_report_only": True, "ai_skip_llm": True,
                    "kind": "daily", "time": "03:00"})
                self.assertEqual(r2.status_code, 200)
                j2 = next(x for x in self.client.get("/api/jobs").json()["jobs"]
                          if x["label"] == "aij2")
                self.assertIn("--ai-report-only", j2["args"])
                self.assertNotIn("--ai-model", j2["args"])

                # skip-llm WITHOUT report-only is rejected (can't decide deletes)
                bad = self.client.post("/api/jobs", json={
                    "name": "aij3", "profile": "pf", "ai_cleanup": True,
                    "ai_skip_llm": True, "kind": "daily", "time": "03:00"})
                self.assertEqual(bad.status_code, 400)

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
