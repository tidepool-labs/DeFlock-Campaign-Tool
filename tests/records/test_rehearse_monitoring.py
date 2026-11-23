"""Offline stage 18 rehearsal contracts, on temporary synthetic inputs."""
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import pipeline_health, rehearse_monitoring as driver
from campaign_tool.records import run_orchestrator, run_report, run_stages

UNITS = ["mail-arrival-stamp.sh", "records-arrival.path", "records-arrival.service", "records-backup-check.service",
         "records-backup-check.timer", "records-health.service", "records-health.timer",
         "records-run.service", "records-run.timer", "run-records.sh", "targets/TEST.json"]
STALE = "health-last_run-no_recent_run"
OUTPUTS = "health-outputs-outputs_degraded"
MAIL = "health-mail_poll-mail_poll_stale"
FAILURE_CODE = "OUTPUTS_SYNTHETIC_FAILURE"


def result(steps, codes=(), manual=()):
    return {"stages": ["18"], "steps": steps, "codes": list(codes), "manual_steps": list(manual)}


def step(name, outputs=(), code="OK"):
    return {"name": name, "code": code, "outputs": list(outputs)}


class RehearseMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.sandbox = self.parent / "sandbox"

    def test_stage_18(self):
        """C1: Real render, distinct alerts, and recovery; C2: bytes bound; C3: no delivery."""
        self.sandbox.mkdir(mode=0o700)
        marker = self.sandbox / run_stages.SANDBOX_MARKER
        touch = Path.touch
        popen = subprocess.Popen
        analyzer = shutil.which("systemd-analyze")
        os_open, io_open = os.open, io.open
        repo = Path(driver.__file__).resolve().parents[2]
        directories = {}

        def check_open(path, *, write=False, directory=False):
            self.assertTrue(path.is_relative_to(self.sandbox)
                            or not write and (path.is_relative_to(repo)
                                              or directory and path in self.sandbox.parents), path)

        def file_open(file, mode="r", *args, **kwargs):
            if not isinstance(file, int):  # Popen wraps its existing pipe descriptors.
                check_open(Path(file).resolve(), write=any(flag in mode for flag in "wax+"))
            return io_open(file, mode, *args, **kwargs)

        def descriptor_open(path, flags, *args, dir_fd=None, **kwargs):
            resolved = ((directories[dir_fd] if dir_fd is not None else Path.cwd()) / path).resolve()
            directory = bool(flags & os.O_DIRECTORY)
            check_open(resolved, write=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)), directory=directory)
            fd = os_open(path, flags, *args, dir_fd=dir_fd, **kwargs)
            if directory:
                directories[fd] = resolved
            return fd

        def first_touch(path, *args, **kwargs):
            self.assertEqual((path, list(self.sandbox.iterdir())), (marker, []))
            return touch(path, *args, **kwargs)

        def syntax_only(argv, *args, **kwargs):
            self.assertIsNotNone(analyzer)
            self.assertEqual(argv[:2], [analyzer, "calendar"])
            self.assertEqual(len(argv), 3)
            return popen(argv, *args, **kwargs)

        # Replacing assess/writer would hide wiring bugs; fence delivery instead.
        with (patch.object(Path, "touch", first_touch), patch.object(subprocess, "Popen", syntax_only),
              patch.object(io, "open", file_open), patch.object(os, "open", descriptor_open),
              patch.object(socket, "socket", side_effect=AssertionError("network forbidden")) as network,
              patch.object(run_orchestrator, "cmd_notify_fallback", side_effect=AssertionError("notify forbidden")) as notify,
              patch.object(run_orchestrator, "execute", side_effect=AssertionError("run forbidden")) as execute):
            actual = driver.run(self.sandbox)
        self.assertEqual((network.call_count, notify.call_count, execute.call_count), (0, 0, 0))
        self.assertEqual((driver.STAGES, type(driver.TITLE), stat.S_IMODE(marker.stat().st_mode)), (("18",), str, 0o600))

        groups = [
            ("render_units", [f"units/{name}" for name in UNITS]),
            ("healthy", ["healthy/health.json"]),
            ("stale_run", ["stale_run/health.json", f"stale_run/{STALE}.json"]),
            ("failed_outputs", ["failed_outputs/health.json", f"failed_outputs/{STALE}.cleared.json",
                                f"failed_outputs/{OUTPUTS}.json"]),
            ("recovered", ["recovered/health.json", f"recovered/{STALE}.cleared.json",
                           f"recovered/{OUTPUTS}.cleared.json"]),
            ("mail_poll_stale", ["mail_poll_stale/health.json", f"mail_poll_stale/{STALE}.cleared.json",
                                 f"mail_poll_stale/{MAIL}.json", f"mail_poll_stale/{OUTPUTS}.cleared.json"]),
            ("mail_poll_recovered", ["mail_poll_recovered/health.json", f"mail_poll_recovered/{STALE}.cleared.json",
                                     f"mail_poll_recovered/{MAIL}.cleared.json",
                                     f"mail_poll_recovered/{OUTPUTS}.cleared.json"]),
        ]
        # Regression: refreshing the poll on every observation never exercises mail loss or clearance.
        self.assertEqual([entry["name"] for entry in actual["steps"]], [name for name, _ in groups])
        self.assertEqual(actual, result([step(name, [self.artifact(path) for path in paths]) for name, paths in groups]))
        for unit, expected in (("health", "hourly"), ("backup-check", "*-*-* 06:10")):
            lines = (self.sandbox / f"units/records-{unit}.timer").read_text().splitlines()
            self.assertEqual([line for line in lines if line.startswith(("OnCalendar=", "Persistent=", "Unit="))],
                             [f"OnCalendar={expected}", "Persistent=true", f"Unit=records-{unit}.service"])

        health = [self.read(f"{name}/health.json") for name, _ in groups[1:]]
        healthy, stale, failed, recovered, mail_stale, mail_recovered = health
        self.assertEqual([(h["schema"], h["state"], h["exit_code"]) for h in health],
                         [("records-health-v1", "ok", 0), ("records-health-v1", "stop", 3),
                          ("records-health-v1", "warn", 1), ("records-health-v1", "ok", 0),
                          ("records-health-v1", "warn", 1), ("records-health-v1", "ok", 0)])
        self.assertEqual(healthy["last_run_finished_at"], stale["last_run_finished_at"])
        self.assertGreater(datetime.fromisoformat(stale["now"]) - datetime.fromisoformat(stale["last_run_finished_at"]),
                           timedelta(hours=pipeline_health.LIMITS["run_max_hours"]))
        stale_alert = {"schema": "records-health-alert-v1", "check": "last_run", "state": "stop",
                       "reason_code": "no_recent_run", "first_seen": stale["now"], "last_seen": stale["now"], "count": 1}
        failed_alert = {"schema": "records-health-alert-v1", "check": "outputs", "state": "warn",
                        "reason_code": "outputs_degraded", "first_seen": failed["now"], "last_seen": failed["now"],
                        "count": 1, "code": FAILURE_CODE, "streak": pipeline_health.LIMITS["outputs_streak"]}
        self.assertEqual(self.read(f"stale_run/{STALE}.json"), stale_alert)
        self.assertEqual(self.read(f"failed_outputs/{OUTPUTS}.json"), failed_alert)
        for observation, state, reason, poll_at in (
            (mail_stale, "warn", "mail_poll_stale", recovered["now"]),
            (mail_recovered, "ok", "mail_poll_recent", mail_recovered["now"]),
        ):
            checks = {check["check"]: check for check in observation["checks"]}
            self.assertEqual(checks.pop("mail_poll"), {"check": "mail_poll", "state": state,
                                                       "reason_code": reason, "since": poll_at})
            self.assertEqual({name: (check["state"], check["reason_code"]) for name, check in checks.items()},
                             {check["check"]: (check["state"], check["reason_code"])
                              for check in recovered["checks"] if check["check"] != "mail_poll"})
        poll_age = datetime.fromisoformat(mail_stale["now"]) - datetime.fromisoformat(recovered["now"])
        self.assertGreater(poll_age, timedelta(hours=pipeline_health.LIMITS["mail_poll_warn_hours"]))
        self.assertLessEqual(poll_age, timedelta(hours=pipeline_health.LIMITS["mail_poll_stop_hours"]))
        self.assertGreater(datetime.fromisoformat(mail_recovered["now"]), datetime.fromisoformat(mail_stale["now"]))
        mail_alert = {"schema": "records-health-alert-v1", "check": "mail_poll", "state": "warn",
                      "reason_code": "mail_poll_stale", "first_seen": mail_stale["now"],
                      "last_seen": mail_stale["now"], "count": 1}
        mail_cleared = dict(mail_alert, state="cleared", cleared_at=mail_recovered["now"])
        self.assertEqual(self.read(f"mail_poll_stale/{MAIL}.json"), mail_alert)
        self.assertEqual(self.read(f"mail_poll_recovered/{MAIL}.cleared.json"), mail_cleared)
        for name in (STALE, OUTPUTS):
            retained = f"{name}.cleared.json"
            before = self.artifact(f"recovered/{retained}")["sha256"]
            for observation in ("mail_poll_stale", "mail_poll_recovered"):
                self.assertEqual(self.artifact(f"{observation}/{retained}")["sha256"], before)
        alerts = self.sandbox / "runtime/TEST/alerts"
        self.assertEqual({p.name: json.loads(p.read_bytes()) for p in alerts.iterdir()},
                         {STALE + ".cleared.json": dict(stale_alert, state="cleared", cleared_at=failed["now"]),
                          OUTPUTS + ".cleared.json": dict(failed_alert, state="cleared", cleared_at=recovered["now"]),
                          MAIL + ".cleared.json": mail_cleared})
        reports = [run_report.validate_run_report(json.loads(p.read_bytes()))
                   for p in sorted((self.sandbox / "runtime/TEST/reports").glob("*.json"))]
        streak = pipeline_health.LIMITS["outputs_streak"]
        self.assertEqual([(r["overall_status"], r["stages"][-2]["error_code"], r["stages"][-1]["status"]) for r in reports],
                         [("ok", None, "skipped")] * streak + [("warn", FAILURE_CODE, "skipped")] * streak
                         + [("ok", None, "skipped")] * 2)
        self.assertEqual(recovered["last_run_finished_at"], reports[-2]["finished_at"])
        self.assertEqual(mail_stale["last_run_finished_at"], reports[-1]["finished_at"])
        self.assertEqual(mail_recovered["last_run_finished_at"], mail_stale["last_run_finished_at"])

    def artifact(self, name):
        return {"path": name, "sha256": hashlib.sha256((self.sandbox / name).read_bytes()).hexdigest()}

    def read(self, name):
        return json.loads((self.sandbox / name).read_bytes())

    def test_render_refusal(self):
        """C1: A changed backup calendar fails rendering evidence and prevents health input writes."""
        self.sandbox.mkdir(mode=0o700)
        read_text = Path.read_text

        def drift(path, *args, **kwargs):
            text = read_text(path, *args, **kwargs)
            if path.name == "records-backup-check.timer.in":
                return text.replace("OnCalendar=*-*-* 06:10", "OnCalendar=daily")
            return text

        with patch.object(Path, "read_text", drift), \
             patch.object(pipeline_health, "assess", side_effect=AssertionError("later step ran")):
            self.assertEqual(driver.run(self.sandbox), result([step("render_units", code="RENDER_UNITS_FAILED")],
                                                              ["RENDER_UNITS_FAILED"]))
        self.assertEqual({path.name for path in self.sandbox.iterdir()}, {run_stages.SANDBOX_MARKER, "units"})

    def test_missing_stale_alert(self):
        """C1: A correct stop report without its alert fails and leaves later scenarios unattempted."""
        self.sandbox.mkdir(mode=0o700)
        # Lose the artifact at the writer boundary, not by changing the assessed state.
        with patch.object(pipeline_health, "write_alerts", side_effect=lambda report, path: path.mkdir(exist_ok=True)) as writer:
            actual = driver.run(self.sandbox)
        self.assertEqual(actual, result([step("render_units", [self.artifact(f"units/{name}") for name in UNITS]),
                                         step("healthy", [self.artifact("healthy/health.json")]),
                                         step("stale_run", [self.artifact("stale_run/health.json")], "STALE_RUN_FAILED")],
                                        ["STALE_RUN_FAILED"]))
        self.assertEqual(writer.call_count, 2)
        self.assertEqual({p.name for p in self.sandbox.iterdir()},
                         {run_stages.SANDBOX_MARKER, "units", "runtime", "records", "healthy", "stale_run"})

    def test_missing_mail_poll_alert_stops_before_recovery(self):
        """C1: A real mail warning must not pass if its open alert was not persisted."""
        self.sandbox.mkdir(mode=0o700)
        put = pipeline_health._put
        suppressed = []

        def drop_mail_alert(directory, name, data):
            if name == MAIL + ".json":
                suppressed.append(data)
            else:
                put(directory, name, data)

        with patch.object(pipeline_health, "_put", side_effect=drop_mail_alert):
            actual = driver.run(self.sandbox)

        self.assertEqual([(entry["name"], entry["code"]) for entry in actual["steps"]],
                         [(name, "OK") for name in ("render_units", "healthy", "stale_run", "failed_outputs", "recovered")]
                         + [("mail_poll_stale", "MAIL_POLL_STALE_FAILED")])
        self.assertEqual(actual["codes"], ["MAIL_POLL_STALE_FAILED"])
        self.assertEqual([(data["check"], data["state"], data["reason_code"]) for data in suppressed],
                         [("mail_poll", "warn", "mail_poll_stale")])
        self.assertEqual(self.read("mail_poll_stale/health.json")["state"], "warn")
        self.assertEqual(actual["steps"][-1]["outputs"], [self.artifact(f"mail_poll_stale/{path}") for path in
                         ("health.json", f"{STALE}.cleared.json", f"{OUTPUTS}.cleared.json")])
        self.assertFalse((self.sandbox / "mail_poll_recovered").exists())

    def test_run_refuses_invalid_sandboxes(self):
        """C3: Direct run cannot start in a missing, non-private, nonempty, or linked root."""
        with self.assertRaises(FileNotFoundError) as caught:
            driver.run(self.sandbox)
        self.assertIs(type(caught.exception), FileNotFoundError)
        self.sandbox.mkdir()
        self.sandbox.chmod(0o755)
        self.check_run_refuses(self.sandbox)
        self.sandbox.chmod(0o700)
        existing = self.sandbox / "existing"
        existing.write_text("keep")
        self.check_run_refuses(self.sandbox)
        self.check_run_refuses(existing)
        link = self.parent / "link"
        link.symlink_to(self.sandbox)
        self.check_run_refuses(link)
        self.assertEqual({p.name: p.read_text() for p in self.sandbox.iterdir()}, {"existing": "keep"})

    def check_run_refuses(self, path):
        with self.assertRaises(ValueError) as caught:
            driver.run(path)
        self.assertIs(type(caught.exception), ValueError)

    def test_cli_returns_json_and_exit_status(self):
        """C1: CLI accepts new/empty roots; either a non-OK step or manual work makes it exit 1."""
        for outcome, status, exists in (
            (result([step("render_units")]), 0, False),
            (result([step("render_units", code="RENDER_UNITS_FAILED")], ["RENDER_UNITS_FAILED"]), 1, True),
            (result([step("render_units")], manual=["Manual step"]), 1, True),
        ):
            with self.subTest(outcome=outcome), redirect_stdout(io.StringIO()) as stdout:
                if exists:
                    self.sandbox.mkdir(mode=0o755)

                def fake_run(path):
                    self.assertEqual((path, stat.S_IMODE(path.stat().st_mode), list(path.iterdir())),
                                     (self.sandbox, 0o700, []))
                    return outcome

                with patch.object(driver, "run", side_effect=fake_run) as launch:
                    self.assertEqual((driver.main(["--sandbox", str(self.sandbox)]), json.loads(stdout.getvalue())),
                                     (status, outcome))
                launch.assert_called_once_with(self.sandbox)
                self.sandbox.rmdir()

    def test_cli_refusals(self):
        """C3: CLI rejects missing/extra flags, nonempty roots, files and symlinks before run."""
        self.sandbox.mkdir()
        existing = self.sandbox / "existing"
        existing.write_text("keep")
        empty = self.parent / "empty"
        empty.mkdir()
        link = self.parent / "symlink"
        link.symlink_to(empty)
        dangling = self.parent / "dangling"
        dangling.symlink_to(self.parent / "nonexistent")
        for argv in ([], ["--sandbox", str(empty), "--extra"], *(["--sandbox", str(p)] for p in
                                                                 (self.sandbox, existing, link, dangling))):
            with (self.subTest(argv=argv), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as stdout,
                  patch.object(driver, "run") as launch, self.assertRaises(SystemExit) as caught):
                driver.main(argv)
            self.assertEqual((type(caught.exception), caught.exception.code, stdout.getvalue(), launch.call_count),
                             (SystemExit, 2, "", 0))
        self.assertEqual((existing.read_text(), list(empty.iterdir()), dangling.is_symlink()), ("keep", [], True))
