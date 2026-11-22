import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_adapt_health as adapter, golive_evidence

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
OBSERVED = "2026-01-02T10:00:00Z"
CHECKS = {
    "arrival_backlog": "none_waiting", "mail_poll": "mail_poll_recent", "last_run": "run_recent",
    "run_failures": "runs_succeeding", "outputs": "outputs_ok", "notify": "notify_ok",
    "schedule": "schedule_ok", "analysis": "analysis_ticking", "console_export": "console_export_current",
    "release_links": "release_links_ok", "disk": "disk_ok",
}


def report():
    return {"schema": "records-health-v1", "now": "2026-01-02T02:00:00-08:00", "state": "ok", "exit_code": 0,
            "waiting": 0, "oldest_waiting_minutes": None, "last_run_finished_at": "2026-01-02T09:00:00Z",
            "checks": [{"check": name, "state": "ok", "reason_code": reason, "since": None}
                       for name, reason in CHECKS.items()]}


def expected(payload, result="pass", codes=(), *, name="health.json", raw=None):
    if raw is None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": "records-golive-evidence-v1", "stage": "18", "kind": "live", "result": result,
            "recorded_at": OBSERVED, "engine": None, "actor": "pipeline", "codes": list(codes),
            "source": {"schema": "records-health-v1", "sha256": hashlib.sha256(raw).hexdigest(), "name": name}}


class HealthEvidenceTests(unittest.TestCase):
    def test_c1_fresh(self):
        """C1: A complete report at the age boundary passes at the observation time."""
        value = report()
        row = adapter.records(value, NOW)
        self.assertEqual(row, expected(value))
        self.assertIsNone(golive_evidence.validate(row))

    def test_c1_stale(self):
        """C1: Do not re-date an old report or ignore a shorter age limit."""
        value = report()
        self.assertEqual(adapter.records(value, NOW, 1.5), expected(value, "fail", ["HEALTH_REPORT_STALE"]))

    def test_c1_incomplete(self):
        """C1: An omitted disk check or an empty inventory cannot certify liveness."""
        for missing in (["disk"], list(CHECKS)):
            with self.subTest(missing=missing):
                value = report()
                value["checks"] = [item for item in value["checks"] if item["check"] not in missing]
                self.assertEqual(adapter.records(value, NOW), expected(value, "fail", [n.upper() for n in missing]))

    def test_c1_unknown_and_unconfigured(self):
        """C1: The aggregate can hide unknown and not-configured checks."""
        value = report()
        value.update(state="warn", exit_code=1)
        value["checks"][1].update(state="unknown", reason_code="mail_poll_unreadable")
        value["checks"][4].update(state="warn", reason_code="outputs_degraded")
        value["checks"][7].update(state="unknown", reason_code="analysis_not_configured")
        value["checks"][8].update(reason_code="console_export_not_configured")
        self.assertEqual(adapter.records(value, NOW), expected(
            value, "fail", ["MAIL_POLL", "OUTPUTS", "ANALYSIS", "CONSOLE_EXPORT"]))

    def test_c1_not_configured(self):
        """C1: A not-configured reason cannot pass even under an OK state."""
        value = report()
        value["checks"][8].update(reason_code="console_export_not_configured")
        self.assertEqual(adapter.records(value, NOW), expected(value, "fail", ["CONSOLE_EXPORT"]))

    def test_c1_refusals(self):
        """C1: Wrong receipt types, clocks and check shapes must not yield evidence."""
        value = report()
        checks = value["checks"]
        cases = (
            ([], NOW, 2), (dict(value, schema="other"), NOW, 2), (dict(value, checks={}), NOW, 2),
            (dict(value, exit_code=False), NOW, 2), (dict(value, now="invalid"), NOW, 2),
            (dict(value, now="2026-01-02T10:00:00"), NOW, 2),
            (dict(value, now="2026-01-02T12:00:01Z"), NOW, 2), (value, NOW.replace(tzinfo=None), 2),
            (dict(value, checks=[None]), NOW, 2), (dict(value, checks=checks + checks[:1]), NOW, 2),
            (dict(value, checks=[dict(checks[0], check="unexpected")]), NOW, 2),
            (dict(value, checks=[dict(checks[0], state="invalid")]), NOW, 2),
            (dict(value, checks=[dict(checks[0], reason_code=None)]), NOW, 2),
            (dict(value, ignored=float("nan")), NOW, 2), (value, NOW, float("nan")),
        )
        for index, (payload, now, limit) in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError) as error:
                    adapter.records(payload, now, limit)
                self.assertIs(type(error.exception), ValueError)

    def test_c2_warn_and_stop(self):
        """C2: Health warnings and stops are observations, not liveness failures."""
        value = report()
        value.update(state="stop", exit_code=3)
        value["checks"][1].update(state="warn", reason_code="mail_poll_stale")
        value["checks"][-1].update(state="stop", reason_code="disk_critical")
        self.assertEqual(adapter.records(value, NOW), expected(value, codes=["MAIL_POLL", "DISK"]))


class HealthCLITests(unittest.TestCase):
    def invoke(self, path, *args):
        out, err = io.StringIO(), io.StringIO()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(NOW.isoformat()).astimezone(tz)

        with redirect_stdout(out), redirect_stderr(err), patch.object(adapter, "datetime", Clock):
            status = adapter.main([str(path), *args])
        return status, out.getvalue(), err.getvalue()

    def test_c3_modes_and_source(self):
        """C3: JSON binds saved bytes and basename; text is the sibling summary."""
        value = report()
        raw = (json.dumps(value, indent=2) + "\n").encode()
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / "capture.json"
            path.write_bytes(raw)
            status, out, err = self.invoke(path, "--json")
            self.assertEqual((status, json.loads(out), err), (0, [expected(value, name="capture.json", raw=raw)], ""))
            self.assertEqual(self.invoke(path), (0, f"18\tlive\tpass\t{OBSERVED}\trecords-health-v1\t\n", ""))
            self.assertEqual(path.read_bytes(), raw)

    def test_c3_negative_evidence(self):
        """C3: A negative result is still printed, unlike malformed input."""
        value = report()
        value["checks"].pop()
        raw = json.dumps(value).encode()
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / "partial.json"
            path.write_bytes(raw)
            status, out, err = self.invoke(path, "--json")
            self.assertEqual((status, json.loads(out), err),
                             (2, [expected(value, "fail", ["DISK"], name="partial.json", raw=raw)], ""))

    def test_c3_bad_input(self):
        """C3: Unreadable, ambiguous or oversized input is refused without leaking its path."""
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / "capture.json"
            refused = (2, "", "HEALTH_EVIDENCE_BAD_INPUT\n")
            self.assertEqual(self.invoke(path, "--json"), refused)
            self.assertEqual(self.invoke(tmp, "--json"), refused)
            for raw in (b'{"checks":[],"checks":[]}', b'{', b' ' * (1024 * 1024 + 1)):
                with self.subTest(size=len(raw)):
                    path.write_bytes(raw)
                    self.assertEqual(self.invoke(path, "--json"), refused)
