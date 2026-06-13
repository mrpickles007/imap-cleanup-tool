"""Unit tests for scheduling: due logic, OS export, and job persistence."""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import scheduler
from imap_cleanup_tool.scheduler import (
    Job, _due, cli_invocation, export_cron, export_windows,
)


class DueTests(unittest.TestCase):
    def test_daily_due_at_exact_time_when_never_run(self):
        job = Job("j", schedule={"kind": "daily", "time": "03:00"})
        now = datetime(2025, 1, 1, 3, 0)
        self.assertTrue(_due(job, now, last=None))

    def test_daily_not_due_at_other_time(self):
        job = Job("j", schedule={"kind": "daily", "time": "03:00"})
        now = datetime(2025, 1, 1, 4, 0)
        self.assertFalse(_due(job, now, last=None))

    def test_daily_not_due_twice_same_day(self):
        job = Job("j", schedule={"kind": "daily", "time": "03:00"})
        now = datetime(2025, 1, 1, 3, 0)
        last = datetime(2025, 1, 1, 3, 0)
        self.assertFalse(_due(job, now, last))

    def test_interval_due_when_never_run(self):
        job = Job("j", schedule={"kind": "interval", "minutes": 60})
        self.assertTrue(_due(job, datetime(2025, 1, 1, 0, 0), last=None))

    def test_interval_respects_elapsed(self):
        job = Job("j", schedule={"kind": "interval", "minutes": 60})
        now = datetime(2025, 1, 1, 2, 0)
        self.assertFalse(_due(job, now, datetime(2025, 1, 1, 1, 30)))
        self.assertTrue(_due(job, now, datetime(2025, 1, 1, 0, 30)))


class ExportTests(unittest.TestCase):
    def test_cron_daily(self):
        job = Job("nightly", args=["--yes"],
                  schedule={"kind": "daily", "time": "03:00"})
        line = export_cron(job)
        self.assertTrue(line.startswith("0 3 * * *"))
        self.assertIn("imap-cleanup-tool job: nightly", line)

    def test_cron_interval(self):
        job = Job("j", schedule={"kind": "interval", "minutes": 15})
        self.assertTrue(export_cron(job).startswith("*/15 * * * *"))

    def test_windows_daily(self):
        job = Job("nightly", schedule={"kind": "daily", "time": "03:00"})
        cmd = export_windows(job)
        self.assertIn('/TN "ImapCleanupTool_nightly"', cmd)
        self.assertIn("/SC DAILY /ST 03:00", cmd)

    def test_windows_interval(self):
        job = Job("j", schedule={"kind": "interval", "minutes": 30})
        self.assertIn("/SC MINUTE /MO 30", export_windows(job))

    def test_cli_invocation_targets_package_module(self):
        cmd = cli_invocation(["--host", "imap.example.com"])
        self.assertIn("imap_cleanup_tool.cli", cmd)
        self.assertIn("--host", cmd)
        self.assertIn(sys.executable.split("\\")[-1].split("/")[-1], cmd)


class PersistenceTests(unittest.TestCase):
    def test_upsert_load_delete_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                self.assertEqual(scheduler.load_jobs(), [])
                scheduler.upsert_job(Job("a", args=["--yes"],
                                         schedule={"kind": "daily"}))
                scheduler.upsert_job(Job("b", schedule={"kind": "daily"}))
                names = {j.name for j in scheduler.load_jobs()}
                self.assertEqual(names, {"a", "b"})

                # upsert replaces same-name job rather than duplicating
                scheduler.upsert_job(Job("a", args=["--expunge"],
                                         schedule={"kind": "daily"}))
                jobs = scheduler.load_jobs()
                self.assertEqual(len(jobs), 2)
                job_a = next(j for j in jobs if j.name == "a")
                self.assertEqual(job_a.args, ["--expunge"])

                scheduler.delete_job("a")
                self.assertEqual(
                    {j.name for j in scheduler.load_jobs()}, {"b"})


if __name__ == "__main__":
    unittest.main()
