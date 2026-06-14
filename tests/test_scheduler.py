"""Unit tests for scheduling: schedule building, OS export, persistence, logs."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import scheduler
from imap_cleanup_tool.scheduler import (
    Job, build_schedule, export_cron, export_windows,
)


class BuildScheduleTests(unittest.TestCase):
    def test_once_requires_date(self):
        with self.assertRaises(ValueError):
            build_schedule("once", time="10:00", date="")

    def test_once_normalises(self):
        s = build_schedule("once", time="9:5", date="2026-07-01")
        self.assertEqual(s, {"kind": "once", "date": "2026-07-01",
                             "time": "09:05"})

    def test_interval_minimum(self):
        with self.assertRaises(ValueError):
            build_schedule("interval", minutes=0)
        self.assertEqual(build_schedule("interval", minutes=15),
                         {"kind": "interval", "minutes": 15})

    def test_hourly_takes_minute_from_time(self):
        self.assertEqual(build_schedule("hourly", time="00:20"),
                         {"kind": "hourly", "minute": 20})

    def test_weekly_validates_day(self):
        self.assertEqual(build_schedule("weekly", day="mon", time="07:30"),
                         {"kind": "weekly", "day": "MON", "time": "07:30"})
        with self.assertRaises(ValueError):
            build_schedule("weekly", day="", time="07:30")

    def test_monthly_range(self):
        self.assertEqual(build_schedule("monthly", day="5", time="01:00"),
                         {"kind": "monthly", "day": 5, "time": "01:00"})
        with self.assertRaises(ValueError):
            build_schedule("monthly", day="31", time="01:00")

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            build_schedule("yearly")


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

    def test_cron_hourly(self):
        job = Job("j", schedule={"kind": "hourly", "minute": 20})
        self.assertTrue(export_cron(job).startswith("20 * * * *"))

    def test_cron_weekly(self):
        job = Job("j", schedule={"kind": "weekly", "day": "MON",
                                 "time": "07:30"})
        self.assertTrue(export_cron(job).startswith("30 7 * * 1"))

    def test_cron_monthly(self):
        job = Job("j", schedule={"kind": "monthly", "day": 5, "time": "01:00"})
        self.assertTrue(export_cron(job).startswith("0 1 5 * *"))

    def test_cron_once_uses_at(self):
        job = Job("j", schedule={"kind": "once", "date": "2026-07-01",
                                 "time": "09:05"})
        line = export_cron(job)
        self.assertIn("at 09:05 2026-07-01", line)

    def test_windows_daily(self):
        job = Job("nightly", schedule={"kind": "daily", "time": "03:00"})
        cmd = export_windows(job)
        self.assertIn('/TN "ImapCleanupTool_nightly"', cmd)
        self.assertIn("/SC DAILY /ST 03:00", cmd)

    def test_windows_interval(self):
        job = Job("j", schedule={"kind": "interval", "minutes": 30})
        self.assertIn("/SC MINUTE /MO 30", export_windows(job))

    def test_windows_weekly(self):
        job = Job("j", schedule={"kind": "weekly", "day": "MON",
                                 "time": "07:30"})
        self.assertIn("/SC WEEKLY /D MON /ST 07:30", export_windows(job))

    def test_windows_monthly(self):
        job = Job("j", schedule={"kind": "monthly", "day": 5, "time": "01:00"})
        self.assertIn("/SC MONTHLY /D 5 /ST 01:00", export_windows(job))

    def test_export_runs_job_by_name(self):
        # The exported OS command must invoke the job by name (quote-safe), not
        # embed the raw --rule/--targets args.
        job = Job("nightly", args=["--rule", 'sender contains "Black Friday"'],
                  schedule={"kind": "daily", "time": "03:00"})
        cmd = export_windows(job)
        self.assertIn("--run-job", cmd)
        self.assertIn("nightly", cmd)
        self.assertNotIn("--rule", cmd)
        self.assertIn("imap_cleanup_tool.cli", export_cron(job))


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


class JobLogTests(unittest.TestCase):
    def test_read_missing_log_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                self.assertEqual(scheduler.read_job_log("nope"), "")

    def test_write_then_read_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                scheduler.job_log_path("j").write_text("hello\n",
                                                       encoding="utf-8")
                self.assertIn("hello", scheduler.read_job_log("j"))

    def test_describe_schedule(self):
        self.assertIn("Weekly", scheduler.describe_schedule(
            {"kind": "weekly", "day": "MON", "time": "07:30"}))
        self.assertIn("Once", scheduler.describe_schedule(
            {"kind": "once", "date": "2026-07-01", "time": "09:05"}))


class RunJobCliTests(unittest.TestCase):
    def test_unknown_job_returns_error_code(self):
        from imap_cleanup_tool import cli
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                self.assertEqual(cli.main(["--run-job", "no-such-job"]), 4)


if __name__ == "__main__":
    unittest.main()
