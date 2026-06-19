"""Unit tests for scheduling: schedule building, OS export, persistence, logs."""

import subprocess
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

    def test_log_is_under_per_profile_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                p = scheduler.job_log_path("nightly", profile="my gmail")
                self.assertEqual(p.parent.name, "my_gmail")   # slugged profile dir
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("ran\n", encoding="utf-8")
                self.assertIn("ran", scheduler.read_job_log(
                    "nightly", profile="my gmail"))

    def test_read_falls_back_to_legacy_flat_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                scheduler.job_log_path("old").write_text("legacy\n",
                                                         encoding="utf-8")
                # asked with a profile but only the flat log exists -> still found
                self.assertIn("legacy", scheduler.read_job_log(
                    "old", profile="acct"))

    def test_delete_job_removes_its_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                scheduler.upsert_job(Job("nightly", args=["--profile", "acct"],
                                         schedule={"kind": "daily"}))
                p = scheduler.job_log_path("nightly", profile="acct")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("logged\n", encoding="utf-8")
                self.assertTrue(p.exists())
                scheduler.delete_job("nightly")
                self.assertFalse(p.exists())   # log gone with the job

    def test_describe_schedule(self):
        self.assertIn("Weekly", scheduler.describe_schedule(
            {"kind": "weekly", "day": "MON", "time": "07:30"}))
        self.assertIn("Once", scheduler.describe_schedule(
            {"kind": "once", "date": "2026-07-01", "time": "09:05"}))


class AtJobTests(unittest.TestCase):
    """One-shot POSIX jobs are tracked via `at`/`atq`/`atrm`."""

    def test_install_at_records_job_number(self):
        def fake_run(cmd, *a, **k):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="",
                stderr="warning: commands will be executed using /bin/sh\n"
                       "job 7 at Wed Jul  1 09:05:00 2026\n")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(scheduler.subprocess, "run",
                                   side_effect=fake_run):
                job = Job("oneoff", schedule={"kind": "once",
                          "date": "2026-07-01", "time": "09:05"})
                msg = scheduler.install_cron(job)
                self.assertIn("#7", msg)
                saved = next(j for j in scheduler.load_jobs()
                             if j.name == "oneoff")
                self.assertEqual(saved.at_id, 7)

    def test_installed_names_includes_queued_at_job(self):
        def fake_run(cmd, *a, **k):
            if cmd[0] == "crontab":
                raise FileNotFoundError
            if cmd[0] == "atq":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="7\tWed Jul  1 09:05:00 2026 a user\n",
                    stderr="")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(scheduler.sys, "platform", "linux"), \
                 mock.patch.object(scheduler.subprocess, "run",
                                   side_effect=fake_run):
                scheduler.upsert_job(Job("oneoff", schedule={"kind": "once",
                    "date": "2026-07-01", "time": "09:05"}, at_id=7))
                self.assertIn("oneoff", scheduler.installed_job_names())

    def test_uninstall_at_runs_atrm_and_clears_id(self):
        calls = []
        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)), \
                 mock.patch.object(scheduler.subprocess, "run",
                                   side_effect=fake_run):
                scheduler.upsert_job(Job("oneoff", schedule={"kind": "once",
                    "date": "2026-07-01", "time": "09:05"}, at_id=7))
                msg = scheduler.uninstall_cron(Job("oneoff", schedule={}))
                self.assertIn("#7", msg)
                self.assertTrue(any(c[0] == "atrm" and c[1] == "7"
                                    for c in calls))
                saved = next(j for j in scheduler.load_jobs()
                             if j.name == "oneoff")
                self.assertIsNone(saved.at_id)


class RunJobCliTests(unittest.TestCase):
    def test_unknown_job_returns_error_code(self):
        from imap_cleanup_tool import cli
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scheduler, "config_dir",
                                   return_value=Path(tmp)):
                self.assertEqual(cli.main(["--run-job", "no-such-job"]), 4)

    def test_ai_cleanup_requires_ai_extra(self):
        from imap_cleanup_tool import cli
        # When litellm (the [ai] extra) is missing, --ai-cleanup is rejected up
        # front (before connecting) with a clear install message.
        with mock.patch.object(cli.importlib.util, "find_spec",
                               return_value=None):
            code = cli.main(["--host", "h", "--user", "u", "--password", "p",
                             "--ai-cleanup"])
        self.assertEqual(code, 3)

    def test_version_flag(self):
        import io
        import contextlib
        from imap_cleanup_tool import cli, __version__
        for flag in ("--version", "-V"):
            out = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stdout(out):
                cli.parse_args([flag])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn(__version__, out.getvalue())


if __name__ == "__main__":
    unittest.main()
