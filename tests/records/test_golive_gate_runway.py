"""Synthetic tests for the gate-runway report (C1-C4); no attended step is performed."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from campaign_tool.records import golive_evidence, golive_gate, golive_gate_order, golive_gate_runway as mod

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
STAMP = "%Y-%m-%dT%H:%M:%SZ"
SWITCH = "s5-engine-switch"
PRIOR = {"type": "prior_gate", "label": "switch GO", "gate_id": SWITCH, "max_age_days": 1}
REHEARSAL = {"type": "evidence", "label": "rehearsal", "stage": "15", "kind": "rehearsal", "max_age_days": 7}
COUNTER = iter(range(10**6))


def rec(stage, kind, result, recorded, name="x", schema="records-golive-evidence-v1"):
    return {"schema": "records-golive-evidence-v1", "stage": stage, "kind": kind, "result": result,
            "recorded_at": recorded.strftime(STAMP), "engine": None, "actor": "operator", "codes": [],
            "source": {"schema": schema, "name": name,
                       "sha256": hashlib.sha256(str(next(COUNTER)).encode()).hexdigest()}}


def gate_go(recorded, result="pass", name=SWITCH):
    return rec("15", "gate", result, recorded, name)


def rehearsal(recorded, result="pass", stage="15", **kwargs):
    return rec(stage, "rehearsal", result, recorded, **kwargs)


def run(*argv, now=NOW):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(list(argv), now=now)
    return code, out.getvalue(), err.getvalue()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = TemporaryDirectory(dir=Path.cwd().resolve())  # private 0700 fixture root
        self.addCleanup(self.root.cleanup)
        self.count = 0

    def ledger(self, *records):
        self.count += 1
        path = Path(self.root.name) / f"ledger{self.count}"
        path.mkdir(mode=0o700)
        for record in records:
            file = path / golive_evidence._filename(record)
            file.write_text(json.dumps(record), encoding="utf-8")
            file.chmod(0o600)
        return str(path)

    def row(self, check, *records, at=NOW):
        return mod.check_row(check, list(records), NOW, at)


class StatusTests(Fixture):
    def test_c2_prior_go_fresh_now_stale_at_target(self):
        # C2/(a): a GO 1 day old at the window but fresh today must be EXPIRES_BEFORE_WINDOW;
        # a report that only evaluates "now" (or ignores `at`) calls it OK and the booking fails later.
        recorded = NOW - timedelta(hours=2)
        row = self.row(PRIOR, gate_go(recorded), at=NOW + timedelta(hours=23))
        self.assertEqual((row["status"], row["last_fresh"]),
                         (mod.EXPIRES, (recorded + timedelta(days=1)).strftime(STAMP[:-1] + ".%fZ")))
        self.assertEqual(self.row(PRIOR, gate_go(recorded), at=NOW + timedelta(hours=21))["status"], mod.OK)

    def test_c2_exact_boundary_instant(self):
        # C2/(b): `_fresh` accepts age == max_age_days exactly and refuses one microsecond more.
        # `<` instead of `<=`, or rounding the limit to whole seconds/days, moves this edge.
        recorded = NOW - timedelta(hours=1)
        edge = recorded + timedelta(days=1)
        statuses = [self.row(PRIOR, gate_go(recorded), at=at)["status"]
                    for at in (edge - timedelta(microseconds=1), edge, edge + timedelta(microseconds=1))]
        self.assertEqual(statuses, [mod.OK, mod.OK, mod.EXPIRES])
        half = {**REHEARSAL, "max_age_days": 1.5}  # float ages use the evaluator's exact decimal limit
        edge = recorded + timedelta(days=1.5)
        self.assertTrue(golive_gate._fresh(rehearsal(recorded), edge, 1.5))
        self.assertEqual([self.row(half, rehearsal(recorded), at=edge + d)["status"]
                          for d in (timedelta(0), timedelta(microseconds=1))], [mod.OK, mod.EXPIRES])

    def test_c2_expiry_past_year_9999_does_not_crash(self):
        # C2/(d): a schema-valid pass dated 9999-12-31 plus a 7-day limit overflows datetime; the
        # report must still say STALE_NOW (the evaluator's answer for a future record) and name
        # the latest representable instant, not raise OverflowError. A limit too large for
        # timedelta on a fresh record clamps the same way and stays OK.
        last = datetime.max.strftime(STAMP[:-1] + ".%fZ")
        far = rehearsal(datetime(9999, 12, 31, 23, 59, 59))
        row = self.row(REHEARSAL, far)
        self.assertFalse(golive_gate._fresh(far, NOW, 7))
        self.assertEqual((row["status"], row["last_fresh"]), (mod.STALE, last))
        recent = rehearsal(NOW - timedelta(hours=1))
        row = self.row({**REHEARSAL, "max_age_days": 10 ** 12}, recent)
        self.assertTrue(golive_gate._fresh(recent, NOW, 10 ** 12))
        self.assertEqual((row["status"], row["last_fresh"]), (mod.OK, last))

    def test_c2_missing_failed_stale(self):
        # C2/(c): each unusable state needs its own label; folding failed/missing into stale (or
        # letting an old fail hide behind a newer pass) misleads the operator.
        old, fresh = NOW - timedelta(days=3), NOW - timedelta(hours=1)
        for check, records, want, last in (
            (PRIOR, [], mod.MISSING, False),
            (PRIOR, [gate_go(fresh, name="other-gate")], mod.MISSING, False),
            (PRIOR, [gate_go(fresh, "fail")], mod.FAILED, False),
            (PRIOR, [gate_go(old)], mod.STALE, True),
            (PRIOR, [gate_go(old, "fail"), gate_go(old)], mod.STALE, True),  # a pass outranks a fail
            (PRIOR, [gate_go(fresh), gate_go(fresh - timedelta(hours=1), "fail")], mod.OK, True),
            (PRIOR, [gate_go(old), gate_go(fresh, name=SWITCH + "-b")], mod.OK, True),  # startswith, as evaluated
            (REHEARSAL, [], mod.MISSING, False),
            (REHEARSAL, [rehearsal(fresh, stage="3")], mod.MISSING, False),
            (REHEARSAL, [rehearsal(old, "fail"), rehearsal(fresh)], mod.OK, True),
            (REHEARSAL, [rehearsal(old), rehearsal(fresh, "fail")], mod.FAILED, False),  # latest record decides
            (REHEARSAL, [rehearsal(NOW - timedelta(days=8))], mod.STALE, True),
            (REHEARSAL, [rehearsal(NOW + timedelta(days=1))], mod.STALE, True),  # a future record is not fresh
            (REHEARSAL, [rehearsal(old), rehearsal(fresh, schema="records-e2e-proof-v1")], mod.OK, True),
            (REHEARSAL, [rehearsal(fresh, "fail", schema="records-e2e-proof-v1"), rehearsal(old)], mod.OK, True),
        ):
            with self.subTest(check=check["type"], want=want, records=len(records)):
                row = self.row(check, *records)
                self.assertEqual(row["status"], want)
                self.assertEqual(row["last_fresh"] is not None, last)


class AgreementTests(Fixture):
    def test_c2_agrees_with_the_evaluator_on_a_matrix(self):
        # C2/(d): the evaluator is the authority. For each ledger and (now, target) pair the report must be
        # what golive_gate.evaluate says at now and at the target; a copied-but-drifted rule (other
        # filter, other boundary, other prior-gate scan) fails here.
        base = datetime(2026, 11, 20, tzinfo=timezone.utc)
        later = base + timedelta(hours=5)
        ledgers = [
            [], [gate_go(base)], [gate_go(base, "fail")], [gate_go(base, "fail"), gate_go(base)],
            [gate_go(base), gate_go(later, "fail")], [gate_go(base, name="other")],
            [gate_go(base, name=SWITCH + "-v2")], [rehearsal(base), rehearsal(later, "fail")],
            [rehearsal(base, "fail"), rehearsal(later)], [rehearsal(later, schema="records-e2e-proof-v1")],
            [rehearsal(base), rehearsal(later, schema="records-e2e-proof-v1")], [rehearsal(base, stage="3")],
        ]
        code = {"EVIDENCE_MATCH": "ok", "EVIDENCE_STALE": "stale", "EVIDENCE_MISSING": "missing",
                "EVIDENCE_FAILED": "failed"}
        checked = 0
        for check in (PRIOR, REHEARSAL, {**REHEARSAL, "max_age_days": 1.5}, {**PRIOR, "max_age_days": 0}):
            spec = {"schema": golive_gate.SPEC_SCHEMA, "gate_id": "probe", "stages": ["15"], "title": "t",
                    "checks": [check]}
            for records in ledgers:
                path = self.ledger(*records)
                for now in (base - timedelta(hours=1), base + timedelta(hours=5), base + timedelta(days=1),
                            base + timedelta(days=1, hours=5), base + timedelta(days=8)):
                    for ahead in (0, 1, 3600, 86400, 5 * 86400):
                        at = now + timedelta(seconds=ahead)
                        said = mod.check_row(check, golive_evidence.load(path), now, at)["status"]
                        then = [code[golive_gate.evaluate(spec, {"evidence": path}, when)["checks"][0]["code"]]
                                for when in (now, at)]
                        want = ((mod.OK if then[1] == "ok" else mod.EXPIRES) if then[0] == "ok"
                                else {"stale": mod.STALE, "missing": mod.MISSING, "failed": mod.FAILED}[then[0]])
                        with self.subTest(check=check, records=len(records), now=now, at=at):
                            self.assertEqual(said, want)
                        checked += 1
        self.assertGreater(checked, 1000)


class CliTests(Fixture):
    def test_c1_reads_only_and_reports_every_registered_check(self):
        # C1: the folder is untouched and no receipt is written (a copied evaluate-and-save main would
        # leave a <gate>.json); every evidence/prior check of the real packs is a row, none from other types.
        path = self.ledger(gate_go(NOW - timedelta(hours=1)), rehearsal(NOW - timedelta(hours=1), stage="3"))
        before = sorted((p.name, p.stat().st_mtime_ns) for p in Path(path).iterdir())
        with mock.patch.object(golive_gate, "_write_receipt") as write:
            code, out, _ = run("--evidence", path, "--at", (NOW + timedelta(hours=1)).isoformat(), "--json")
        write.assert_not_called()
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in Path(path).iterdir()), before)
        self.assertEqual(sorted(p.name for p in Path(self.root.name).iterdir()), ["ledger1"])
        rows = json.loads(out)["rows"]
        want = sum(1 for d in golive_gate_order.registry().values()
                   for c in d["checks"] if c["type"] in ("evidence", "prior_gate"))
        self.assertEqual((code, len(rows)), (1, want))
        self.assertTrue({r["check"] for r in rows} <= {"evidence", "prior_gate"})

    def test_c1_gate_filter_and_s8_scenario(self):
        # C1: --gate limits rows to that gate; the S8 case from the card (switch GO 1 day, rehearsals 7 days).
        recorded = NOW - timedelta(hours=1)
        path = self.ledger(gate_go(recorded), rehearsal(recorded, stage="3"), rehearsal(recorded, stage="18"))
        code, out, _ = run("--evidence", path, "--at", (NOW + timedelta(hours=2)).isoformat(),
                           "--gate", "s8-units", "--json")
        rows = json.loads(out)["rows"]
        self.assertEqual({r["gate_id"] for r in rows}, {"s8-units"})
        self.assertEqual(sorted((r["check"], r["status"]) for r in rows),
                         [("evidence", "OK"), ("evidence", "OK"), ("prior_gate", "OK")])
        code, out, _ = run("--evidence", path, "--at", (NOW + timedelta(hours=24)).isoformat(), "--gate", "s8-units")
        self.assertEqual(code, 1)
        self.assertIn("s8-units: EXPIRES_BEFORE_WINDOW prior_gate s5-engine-switch", out)

    def test_c3_exit_zero_only_when_every_row_is_ok(self):
        # C3: exit 0 needs all rows OK; returning 0 for "mostly fine" lets a booking proceed on stale input.
        fresh = self.ledger(rehearsal(NOW - timedelta(hours=1)))
        args = ("--gate", SWITCH, "--at", (NOW + timedelta(days=1)).isoformat())
        self.assertEqual(run("--evidence", fresh, *args)[0], 0)
        late = (NOW + timedelta(days=7)).isoformat()
        self.assertEqual(run("--evidence", fresh, "--gate", SWITCH, "--at", late)[0], 1)
        self.assertEqual(run("--evidence", self.ledger(), *args)[0], 1)

    def test_c3_refusals_exit_two(self):
        # C3: a past, naive, unparsable or >30-day target (or unknown gate / unreadable folder) is an error,
        # not a report; exactly 30 days ahead and exactly now are still allowed.
        path = self.ledger()
        iso = lambda delta: (NOW + delta).isoformat()
        for bad in (iso(timedelta(microseconds=-1)), iso(timedelta(days=-3)), iso(timedelta(days=30, microseconds=1)),
                    (NOW + timedelta(days=1)).replace(tzinfo=None).isoformat(), "tomorrow", "",
                    # a valid offset-bearing time whose UTC instant falls outside datetime's range must exit 2,
                    # not escape as an OverflowError traceback (exit 1)
                    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(at=bad):
                code, out, err = run("--evidence", path, "--at", bad)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err)
        for ok in (iso(timedelta(0)), iso(timedelta(days=30))):
            self.assertEqual(run("--evidence", path, "--at", ok)[0], 1)
        self.assertEqual(run("--evidence", path, "--at", iso(timedelta(0)), "--gate", "nope")[0], 2)
        self.assertEqual(run("--evidence", path + "-missing", "--at", iso(timedelta(0)))[0], 2)
        self.assertEqual(run("--at", iso(timedelta(0)))[0], 2)

    def test_c3_offset_target_is_compared_as_an_instant(self):
        # C3: 13:00+01:00 is 12:00Z (allowed, "now"); treating the wall clock as UTC would refuse the same instant.
        path = self.ledger()
        self.assertEqual(run("--evidence", path, "--at", "2026-11-24T13:00:00+01:00")[0], 1)
        self.assertEqual(run("--evidence", path, "--at", "2026-11-24T12:00:00Z")[0], 1)
        self.assertEqual(run("--evidence", path, "--at", "2026-11-24T11:59:59Z")[0], 2)

    def test_c4_output_disclaims_authority(self):
        # C4: both forms state it is not a booking, receipt or authorization; dropping the note makes a
        # list of OK rows look like permission to proceed.
        path = self.ledger()
        for extra in ((), ("--json",)):
            code, out, _ = run("--evidence", path, "--at", NOW.isoformat(), *extra)
            for phrase in ("not a booking, a receipt or an authorization", "performs and schedules nothing"):
                self.assertIn(phrase, out)
        self.assertEqual(json.loads(out)["note"], mod.NOTE)
        self.assertFalse(os.path.exists(Path(self.root.name) / "s5-engine-switch.json"))


if __name__ == "__main__":
    unittest.main()
