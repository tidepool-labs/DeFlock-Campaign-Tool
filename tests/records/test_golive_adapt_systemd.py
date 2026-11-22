"""Synthetic readbacks; exercise the real adapter and its module entry point."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
from itertools import product
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from campaign_tool.records import golive_adapt_systemd as adapter, golive_evidence


CAPTURE = "records-unit-status-capture-v1"
STAMP = "2026-01-02T03:04:05Z"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ARRIVAL = "records-arrival.path"
HEALTH = "records-health.timer"
BACKUP = "records-backup-check.timer"
CONSOLE = "records-console-check.timer"
PROFILE = "a" * 64
INVOCATION = "b" * 32
BINDING = dict(schema="records-systemd-invocation-v1", invocation_id=INVOCATION,
               job_id="c" * 32, profile_sha256=PROFILE)
INTENT = dict(schema="records-systemd-operation-v1", purpose="run",
              invocation_id=INVOCATION, profile_sha256=PROFILE)
MANAGED = {"schema": "records-systemd-managed-v1"}
HOLD = {"schema": "records-systemd-hold-v1", "status": "held"}
CLOSED = dict(BINDING, schema="records-systemd-not-allocated-v1", status="not_allocated")
ERROR = "invalid systemd receipt or unit status capture"


def unit(name, enabled=True, active=True):
    return dict(name=name, enabled=enabled, active=active)


def capture(*units, **overrides):
    return dict({"schema": CAPTURE, "observed_at": STAMP, "units": list(units)}, **overrides)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def receipt(self, value, name="fixture.json", *, canonical=False):
        path = self.root / name
        if isinstance(value, bytes):
            path.write_bytes(value)
        elif canonical:
            path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        else:
            path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def evidence(self, path, stage, result, codes=(), stamp=STAMP):
        return {
            "schema": "records-golive-evidence-v1", "stage": stage, "kind": "live",
            "result": result, "recorded_at": stamp, "engine": None, "actor": "operator",
            "source": {"schema": CAPTURE, "name": path.name, "sha256": sha256(path.read_bytes()).hexdigest()},
            "codes": list(codes),
        }

    def assertInvalid(self, path):
        with self.assertRaises(ValueError) as caught:
            adapter.records([path], NOW)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(str(caught.exception), ERROR)

    def test_c1_arrival_both_flags_required(self):
        """C1: Arrival alone yields only stage 3, even when enabled or active is false."""
        for enabled, active in product((True, False), repeat=2):
            with self.subTest(enabled=enabled, active=active):
                path = self.receipt(capture(unit(ARRIVAL, enabled, active), unit("other.path")))
                expected = (self.evidence(path, "3", "pass") if enabled and active else
                            self.evidence(path, "3", "fail", ["UNIT_NOT_ACTIVE_RECORDS_ARRIVAL_PATH"]))
                self.assertEqual(adapter.records([path], NOW), [expected])

    def test_c1_timer_matrix(self):
        """C1: Every monitored timer is necessary; unrelated timers cannot fill gaps."""
        missing = ("UNIT_MISSING_RECORDS_HEALTH_TIMER", "UNIT_MISSING_RECORDS_BACKUP_CHECK_TIMER",
                   "UNIT_MISSING_RECORDS_CONSOLE_CHECK_TIMER")
        inactive = ("UNIT_NOT_ACTIVE_RECORDS_HEALTH_TIMER", "UNIT_NOT_ACTIVE_RECORDS_BACKUP_CHECK_TIMER",
                    "UNIT_NOT_ACTIVE_RECORDS_CONSOLE_CHECK_TIMER")
        states = (None, (True, True), (True, False), (False, True), (False, False))
        for triple in product(states, repeat=3):
            with self.subTest(states=triple):
                units = [unit(name, *state) for name, state in zip((HEALTH, BACKUP, CONSOLE), triple)
                         if state is not None]
                path = self.receipt(capture(unit("other.timer"), *reversed(units)))
                codes = [missing[i] if state is None else inactive[i]
                         for i, state in enumerate(triple) if state != (True, True)]
                expected = [self.evidence(path, "18", "fail" if codes else "pass", codes)] if units else []
                self.assertEqual(adapter.records([path], NOW), expected)

    def test_c1_two_families_and_evidence_schema(self):
        """C1: Shared capture binds both independent verdicts to the exact raw bytes."""
        earlier = "2026-01-01T23:59:59Z"
        path = self.receipt(capture(unit(CONSOLE), unit(BACKUP), unit(HEALTH), unit(ARRIVAL, False),
                                    observed_at=earlier), "two-families.json")
        rows = adapter.records([str(path)], NOW)
        self.assertEqual(rows, [self.evidence(path, "3", "fail", ["UNIT_NOT_ACTIVE_RECORDS_ARRIVAL_PATH"], earlier),
                                self.evidence(path, "18", "pass", stamp=earlier)])
        for row in rows:
            self.assertIsNone(golive_evidence.validate(row))

    def test_c1_separate_snapshots_never_combine(self):
        """C1: Even complementary snapshots cannot jointly pass stage 18."""
        a = self.receipt(capture(unit(HEALTH)), "a.json")
        b = self.receipt(capture(unit(BACKUP), unit(CONSOLE)), "b.json")
        self.assertEqual(adapter.records([a, b], NOW), [
            self.evidence(a, "18", "fail", ["UNIT_MISSING_RECORDS_BACKUP_CHECK_TIMER",
                                             "UNIT_MISSING_RECORDS_CONSOLE_CHECK_TIMER"]),
            self.evidence(b, "18", "fail", ["UNIT_MISSING_RECORDS_HEALTH_TIMER"]),
        ])

    def test_c1_empty_inputs_and_capture(self):
        """C1: An empty inventory cannot imply that any monitored family was evaluated."""
        self.assertEqual(adapter.records([self.receipt(capture())], NOW), [])
        self.assertEqual(adapter.records([], NOW), [])

    def test_c2_capture_shape(self):
        """C2: Reject extra, absent, malformed or repeated fields instead of guessing."""
        valid = capture(unit(ARRIVAL))
        u = unit(ARRIVAL)
        cases = {
            "unknown-root-field": dict(valid, target="other"),
            "unknown-schema": dict(valid, schema="other"),
            "wrong-root-type": [valid],
            "wrong-units-type": dict(valid, units=u),
            "wrong-unit-type": capture(ARRIVAL),
            "unknown-unit-field": capture(dict(u, status="active")),
            "wrong-name-type": capture(dict(u, name=1)),
            "empty-name": capture(dict(u, name="")),
            "integer-enabled": capture(dict(u, enabled=1)),
            "string-active": capture(dict(u, active="true")),
            "duplicate-name": capture(u, dict(u, active=False)),
            "duplicate-root-key": b'{"schema":"ignored",' + json.dumps(valid).encode()[1:],
            "duplicate-unit-key": json.dumps(valid).encode().replace(b'"active": true', b'"active": false, "active": true'),
        }
        cases.update({f"missing-root-{key}": {k: v for k, v in valid.items() if k != key} for key in valid})
        cases.update({f"missing-unit-{key}": capture({k: v for k, v in u.items() if k != key}) for key in u})
        for label, value in cases.items():
            with self.subTest(case=label):
                self.assertInvalid(self.receipt(value))

    def test_c2_observation_clock(self):
        """C2: Canonical UTC calendar seconds only, never after the supplied clock."""
        for stamp in (None, "2026-01-02T03:04:06Z", "2026-02-30T03:04:05Z",
                      "2026-1-02T03:04:05Z", "2026-01-02T03:04:05+00:00",
                      "2026-01-02T03:04:05.000Z", STAMP + "\n"):
            with self.subTest(stamp=stamp):
                self.assertInvalid(self.receipt(capture(unit(ARRIVAL), observed_at=stamp)))

    def test_c2_unreadable_or_invalid_json(self):
        """C2: I/O and JSON errors surface as ValueError, not raw private diagnostics."""
        for value in (b'{"schema":', b"\xff", b'{"units": NaN}'):
            with self.subTest(raw=value):
                self.assertInvalid(self.receipt(value))
        self.assertInvalid(self.root / "missing.json")
        self.assertInvalid(self.root)
        self.assertInvalid(self.receipt(capture(unit(ARRIVAL)), "not\\a-basename.json"))

    def test_c2_journal_markers_are_not_evidence(self):
        """C2: Actual-shaped operation, mode, invocation, hold and closure yield no rows."""
        values = (INTENT, dict(INTENT, purpose="stop"), dict(INTENT, purpose="hold"),
                  MANAGED, BINDING, HOLD, CLOSED)
        markers = [self.receipt(value, f"marker-{i}.json", canonical=True) for i, value in enumerate(values)]
        self.assertEqual(adapter.records(markers, NOW), [])
        path = self.receipt(capture(unit(ARRIVAL)))
        self.assertEqual(adapter.records([*markers, path], NOW), [self.evidence(path, "3", "pass")])

    def test_c2_journal_reader_refusals(self):
        """C2: Schema labels, intent and invocation fields cannot bypass the owning readers."""
        cases = (
            dict(INTENT, purpose="install"), dict(INTENT, purpose=[]),
            dict(INTENT, invocation_id="bad"), dict(INTENT, profile_sha256="bad"),
            dict(INTENT, units=[unit(ARRIVAL)]), dict(MANAGED, enabled=True),
            dict(BINDING, job_id="bad"), dict(BINDING, active=True),
            dict(HOLD, status="released"), dict(CLOSED, status="allocated"),
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertInvalid(self.receipt(value, canonical=True))
        self.assertInvalid(self.receipt(MANAGED))  # _mode requires the exact persisted bytes.

    def cli(self, *args):
        name = "campaign_tool.records.golive_adapt_systemd"
        stdout, stderr = StringIO(), StringIO()
        # Run the -m entry point in-process without a second import of a cached module.
        with patch.dict(sys.modules), patch.object(sys, "argv", [name, *args]):
            sys.modules.pop(name, None)
            with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exited:
                runpy.run_module(name, run_name="__main__", alter_sys=True)
        return exited.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_c3_default_output_and_exit(self):
        """C3: Default CLI prints whole records, including evaluated failures."""
        path = self.receipt(capture(unit(ARRIVAL, active=False)))
        expected = self.evidence(path, "3", "fail", ["UNIT_NOT_ACTIVE_RECORDS_ARRIVAL_PATH"])
        code, stdout, stderr = self.cli(str(path))
        self.assertEqual((code, [json.loads(line) for line in stdout.splitlines()], stderr), (0, [expected], ""))

    def test_c3_json_output_and_exit(self):
        """C3: JSON mode wraps records in one array and accepts multiple inputs."""
        path = self.receipt(capture(unit(ARRIVAL)))
        intent = self.receipt(INTENT, "intent.json", canonical=True)
        code, stdout, stderr = self.cli(str(path), str(intent), "--json")
        self.assertEqual((code, json.loads(stdout), stderr), (0, [self.evidence(path, "3", "pass")], ""))
        self.assertEqual(self.cli(str(intent), "--json"), (0, "[]\n", ""))

    def test_c3_invalid_later_input_no_partial_output(self):
        """C3: A later bad input exits 2 without emitting earlier evidence or private text."""
        good = self.receipt(capture(unit(ARRIVAL)), "good.json")
        bad = self.receipt(b"not JSON", "bad.json")
        self.assertEqual(self.cli(str(good), str(bad), "--json"), (2, "", f"golive_adapt_systemd: {ERROR}\n"))
