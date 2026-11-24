"""Offline acceptance for the rehearsal runway (contract items C1-C4), synthetic evidence only."""

import inspect
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_catalog, golive_expiry, golive_ledger

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
AGE = timedelta(days=14)
SEC = timedelta(seconds=1)
NEEDED = [stage for stage, rule in golive_catalog.STAGES.items() if rule.needs_rehearsal]


def z(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(stage, result="pass", stamp=None, *, kind="rehearsal", schema="synthetic-v1", digit="c"):
    return dict(schema="records-golive-evidence-v1", stage=stage, kind=kind, result=result,
                recorded_at=stamp or z(NOW), engine=None, actor="operator", codes=[],
                source=dict(schema=schema, sha256=digit * 64, name="synthetic.json"))


def expiring(stage, delta, result="pass", **kwargs):
    """A record whose expiry lands exactly ``delta`` from NOW."""
    return record(stage, result, z(NOW + delta - AGE), **kwargs)


def row(stage, status, recorded=None, expires=None):
    return dict(stage=stage, status=status, recorded_at=recorded, expires_at=expires)


def one(records, within_days=3, stage="2", now=NOW):
    return next(r for r in golive_expiry.runway(records, now, within_days) if r["stage"] == stage)


def ledger_cell(records, stage="2"):
    return next(r for r in golive_ledger.build([], records, NOW) if r["stage"] == stage)["rehearsed"]


class RunwayTests(unittest.TestCase):
    def test_c1_rows_cover_needed_stages_in_catalogue_order(self):
        """C1: one row per rehearsal-needing stage, catalogue order; a hand-listed or all-stage table breaks this."""
        rows = golive_expiry.runway([], NOW, 3)
        self.assertEqual(rows, [row(stage, "MISSING") for stage in NEEDED])
        self.assertTrue({"1", "17b"}.isdisjoint(row["stage"] for row in rows))  # the catalogue's no-rehearsal stages

    def test_c1_exactly_14_days_is_still_valid_one_second_later_is_expired(self):
        """C1: the 14-day edge; an off-by-one (<= vs <) flips the boundary row."""
        self.assertEqual(golive_expiry._REHEARSAL_AGE, timedelta(days=14))
        self.assertIs(golive_expiry._REHEARSAL_AGE, golive_ledger._REHEARSAL_AGE)  # imported, not redefined
        self.assertEqual(one([record("2", stamp="2026-11-09T12:00:00Z")]),
                         row("2", "EXPIRES_SOON", "2026-11-09T12:00:00Z", "2026-11-23T12:00:00Z"))
        self.assertEqual(one([record("2", stamp="2026-11-09T11:59:59Z")]),
                         row("2", "EXPIRED", "2026-11-09T11:59:59Z", "2026-11-23T11:59:59Z"))
        self.assertEqual(one([record("2", stamp="2026-11-09T12:00:01Z")]),
                         row("2", "EXPIRES_SOON", "2026-11-09T12:00:01Z", "2026-11-23T12:00:01Z"))

    def test_c1_within_days_boundary_matrix(self):
        """C1: exactly within_days is EXPIRES_SOON, one second beyond is OK (0 and 13 included)."""
        for days in (0, 1, 3, 13):
            window = timedelta(days=days)
            cases = [(-SEC, "EXPIRED"), (timedelta(0), "EXPIRES_SOON"), (window, "EXPIRES_SOON"), (window + SEC, "OK")]
            if days:
                cases += [(SEC, "EXPIRES_SOON"), (window - SEC, "EXPIRES_SOON")]
            for delta, status in cases:
                with self.subTest(days=days, delta=str(delta)):
                    rec = expiring("2", delta)
                    self.assertEqual(one([rec], days), row("2", status, rec["recorded_at"], z(NOW + delta)))

    def test_c1_window_at_or_beyond_the_rehearsal_age_never_reports_ok(self):
        """C1/C2: a window >= 14 d covers every valid expiry, so only EXPIRED/EXPIRES_SOON/FUTURE_DATED remain."""
        for days in (14, 30):
            for delta, status in ((-SEC, "EXPIRED"), (timedelta(0), "EXPIRES_SOON"), (AGE, "EXPIRES_SOON"),
                                  (AGE + SEC, "FUTURE_DATED")):
                with self.subTest(days=days, delta=str(delta)):
                    rec = expiring("2", delta)
                    self.assertEqual(one([rec], days), row("2", status, rec["recorded_at"], z(NOW + delta)))

    def test_c1_agrees_with_ledger_at_the_boundary(self):
        """C1: EXPIRED exactly where the ledger says missing/STALE; anything else is a ledger pass."""
        for delta in (-2 * SEC, -SEC, timedelta(0), SEC, 2 * SEC):
            with self.subTest(delta=str(delta)):
                rec = expiring("2", delta)
                stale = ledger_cell([rec]) == dict(status="missing", recorded_at=rec["recorded_at"], reason="STALE")
                self.assertEqual(one([rec])["status"] == "EXPIRED", stale)
                if not stale:
                    self.assertEqual(ledger_cell([rec]), dict(status="pass", recorded_at=rec["recorded_at"]))

    def test_c1_failed_missing_and_newest_record_wins(self):
        """C1: newest fail is FAILED even beside an older pass; a newer pass clears an older fail (healthy control)."""
        old, new = z(NOW - 3 * timedelta(days=2)), z(NOW - timedelta(days=1))
        failed = one([record("2", stamp=old, digit="a"), record("2", "fail", new, digit="b")])
        self.assertEqual(failed, row("2", "FAILED", new, z(NOW - timedelta(days=1) + AGE)))
        healed = one([record("2", "fail", old, digit="a"), record("2", stamp=new, digit="b")])
        self.assertEqual(healed, row("2", "OK", new, z(NOW - timedelta(days=1) + AGE)))
        self.assertEqual(one([record("3")], stage="2"), row("2", "MISSING"))

    def test_c1_stale_failure_matches_ledger_stale_not_failed(self):
        """C1: the ledger checks staleness before the result, so an old fail is EXPIRED; a fail one second inside is FAILED."""
        for delta, status in ((-SEC, "EXPIRED"), (timedelta(0), "FAILED")):
            with self.subTest(delta=str(delta)):
                rec = expiring("2", delta, "fail")
                self.assertEqual(one([rec]), row("2", status, rec["recorded_at"], z(NOW + delta)))
        self.assertEqual(ledger_cell([expiring("2", -SEC, "fail")])["reason"], "STALE")

    def test_c1_only_rehearsal_kind_counts(self):
        """C1: newer gate/live records must not stand in for a rehearsal."""
        records = [record("2", kind=kind) for kind in ("gate", "live")]
        self.assertEqual(one(records), row("2", "MISSING"))

    def test_c1_now_is_normalised_like_the_ledger(self):
        """C1: naive now means UTC; an offset now compares as the same instant."""
        rec = expiring("2", timedelta(days=3))
        expected = row("2", "EXPIRES_SOON", rec["recorded_at"], z(NOW + timedelta(days=3)))
        self.assertEqual(one([rec], now=NOW.replace(tzinfo=None)), expected)
        self.assertEqual(one([rec], now=NOW.astimezone(timezone(timedelta(hours=-8)))), expected)

    def test_c1_within_days_is_validated(self):
        """C1: only integers 0-30 are accepted; a bool, float or string must not silently widen the window."""
        for bad in (-1, 31, True, 3.0, "3", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                golive_expiry.runway([], NOW, bad)

    def test_c1_year_9999_record_does_not_crash(self):
        """C1: expiry arithmetic overflow on a far-future stamp must not take the whole report down."""
        self.assertEqual(one([record("2", stamp="9999-12-31T23:59:59Z")]),
                         row("2", "FUTURE_DATED", "9999-12-31T23:59:59Z", "9999-12-31T23:59:59Z"))

    def test_c2_e2e_proof_rehearsals_are_ignored_like_the_ledger(self):
        """C2: a newer e2e-proof pass/fail neither qualifies nor fails a stage; ledger agrees."""
        base, proof = expiring("2", timedelta(days=10), digit="a"), z(NOW)
        for result in ("pass", "fail"):
            with self.subTest(result=result):
                records = [base, record("2", result, proof, schema="records-e2e-proof-v1", digit="b")]
                self.assertEqual(one(records), row("2", "OK", base["recorded_at"], z(NOW + timedelta(days=10))))
                self.assertEqual(ledger_cell(records), dict(status="pass", recorded_at=base["recorded_at"]))
        alone = [record("2", schema="records-e2e-proof-v1")]
        self.assertEqual(one(alone), row("2", "MISSING"))
        self.assertEqual(ledger_cell(alone)["status"], "missing")

    def test_c2_future_dated_is_not_ok(self):
        """C2: a record newer than now is FUTURE_DATED at any window; exactly now is not future."""
        stamp = z(NOW + SEC)
        for days in (0, 3, 30):
            with self.subTest(days=days):
                self.assertEqual(one([record("2", stamp=stamp)], days), row("2", "FUTURE_DATED", stamp, z(NOW + SEC + AGE)))
        self.assertEqual(one([record("2", stamp=z(NOW))]), row("2", "OK", z(NOW), z(NOW + AGE)))
        newest_future = [record("2", stamp=z(NOW - SEC), digit="a"), record("2", stamp=stamp, digit="b")]
        self.assertEqual(one(newest_future)["status"], "FUTURE_DATED")  # newest record decides, not the older good one
        self.assertEqual(one([record("2", "fail", stamp)])["status"], "FAILED")  # a future fail stays a failure


class CliTests(unittest.TestCase):
    def setUp(self):
        self.fresh()

    def fresh(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.evidence = Path(folder.name).resolve() / "golive-evidence"
        self.evidence.mkdir(mode=0o700)

    def write(self, data):
        name = f"{data['recorded_at'].replace('-', '').replace(':', '')}-{data['stage']}-{data['kind']}-{data['source']['sha256'][:8]}.json"
        path = self.evidence / name
        path.touch(mode=0o600)
        path.write_text(json.dumps(data), encoding="utf-8")

    def write_all(self, delta=timedelta(days=10), overrides=None, skip=()):
        for index, stage in enumerate(NEEDED):
            if stage not in skip:
                self.write((overrides or {}).get(stage) or expiring(stage, delta, digit="0123456789abcdef"[index % 16]))

    def invoke(self, *extra, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = golive_expiry.main(["--evidence", str(self.evidence), *extra], now=now)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_c3_exit_zero_only_when_every_row_is_ok(self):
        """C3: all-OK exits 0 and prints the count line; ignoring a status class would hide a lapse."""
        self.write_all()
        code, out, _ = self.invoke()
        self.assertEqual((code, out.splitlines()[-1]),
                         (0, f"{len(NEEDED)} rehearsals: {len(NEEDED)} OK, 0 EXPIRES_SOON, 0 EXPIRED, 0 FAILED, 0 MISSING, 0 FUTURE_DATED"))

    def test_c3_exit_one_for_each_non_ok_status(self):
        """C3: each of the five non-OK statuses alone must flip the exit code to 1."""
        cases = dict(EXPIRES_SOON=expiring("2", timedelta(days=3)), EXPIRED=expiring("2", -SEC),
                     FAILED=expiring("2", timedelta(days=10), "fail"), FUTURE_DATED=record("2", stamp=z(NOW + SEC)))
        for status, rec in cases.items():
            with self.subTest(status=status):
                self.fresh()
                self.write_all(overrides={"2": rec})
                code, out, _ = self.invoke()
                self.assertEqual(code, 1)
                self.assertIn(f"\n2 | {status} | ", out)
        self.fresh()
        self.write_all(skip=("2",))
        code, out, _ = self.invoke()
        self.assertEqual((code, "\n2 | MISSING | - | -\n" in out), (1, True))

    def test_c3_default_window_is_three_days_and_flag_overrides(self):
        """C3: default 3; a 3-day expiry is EXPIRES_SOON by default and OK with --within-days 2."""
        self.write_all(timedelta(days=3))
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke("--within-days", "2")[0], 0)
        self.assertEqual(self.invoke("--within-days", "3")[0], 1)

    def test_c3_rows_ordered_by_expiry_missing_last_ties_in_catalogue_order(self):
        """C3: soonest expiry first, missing last, equal expiries keep catalogue order."""
        sooner, same = expiring("5", timedelta(days=1)), timedelta(days=9)
        gone = NEEDED[1]
        self.write_all(same, {"5": sooner}, skip=(gone,))
        code, out, _ = self.invoke("--json")
        stages = [r["stage"] for r in json.loads(out)["rows"]]
        self.assertEqual(stages, ["5"] + [s for s in NEEDED if s not in ("5", gone)] + [gone])
        self.assertEqual(code, 1)
        text = self.invoke()[1].splitlines()
        self.assertEqual(text[0], "Stage | Status | Recorded | Expires")
        self.assertEqual(text[1], f"5 | EXPIRES_SOON | {sooner['recorded_at']} | {z(NOW + timedelta(days=1))}")
        self.assertEqual(text[-2], f"{gone} | MISSING | - | -")

    def test_c3_json_output_carries_rows_counts_and_window(self):
        """C3: --json mirrors runway rows exactly plus counts and the window used."""
        rec = expiring("2", timedelta(days=5))
        self.write(rec)
        data = json.loads(self.invoke("--json", "--within-days", "7")[1])
        self.assertEqual(data["within_days"], 7)
        self.assertEqual(data["counts"]["EXPIRES_SOON"], 1)
        self.assertEqual(data["counts"]["MISSING"], len(NEEDED) - 1)
        self.assertEqual(data["rows"][0], row("2", "EXPIRES_SOON", rec["recorded_at"], z(NOW + timedelta(days=5))))

    def test_c3_mixed_year_expiries_are_fixed_width_and_sort_by_year(self):
        """C3: a year < 1000 stamp must keep four digits (strftime %Y drops them on glibc), so the string sort puts
        0002 before 1000 before 2026; an unpadded "2-01-15..." would sort last and break the contract format."""
        early, mid, late = "0002-01-01T00:00:00Z", "1000-01-01T00:00:00Z", "2026-11-20T12:00:00Z"
        for stage, stamp, digit in (("2", late, "a"), ("3", early, "b"), ("5", mid, "c")):
            self.write(record(stage, stamp=stamp, digit=digit))
        code, out, _ = self.invoke("--json")
        rows = {r["stage"]: r for r in json.loads(out)["rows"]}
        self.assertEqual(rows["3"], row("3", "EXPIRED", early, "0002-01-15T00:00:00Z"))
        self.assertEqual(rows["5"], row("5", "EXPIRED", mid, "1000-01-15T00:00:00Z"))
        self.assertEqual(rows["2"], row("2", "OK", late, "2026-12-04T12:00:00Z"))
        self.assertEqual([r["stage"] for r in json.loads(out)["rows"]][:3], ["3", "5", "2"])
        self.assertEqual(code, 1)
        text = self.invoke()[1]
        self.assertIn("\n3 | EXPIRED | 0002-01-01T00:00:00Z | 0002-01-15T00:00:00Z\n", text)

    def test_c3_exit_two_on_unreadable_directory_or_bad_records_or_bad_window(self):
        """C3: an unreadable directory, a corrupt record or an out-of-range window is exit 2, never a partial report."""
        self.evidence.joinpath("20261101T000000Z-2-rehearsal-cccccccc.json").write_text("{", encoding="utf-8")
        code, out, err = self.invoke()
        self.assertEqual((code, out, err.startswith("golive_expiry: ")), (2, "", True))
        missing = self.evidence / "absent"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(golive_expiry.main(["--evidence", str(missing)], now=NOW), 2)
        self.assertIn("unreadable evidence directory", err.getvalue())
        for bad in ("-1", "31", "3.5", "abc", "", "1e1", "+3", "100"):
            with self.subTest(bad=bad):
                self.assertEqual(self.invoke("--within-days", bad)[0], 2)

    def test_c4_read_only_and_no_ambient_clock(self):
        """C4: running leaves the folder byte- and mtime-identical, and an injected now never reads the clock."""
        self.write_all()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.evidence.iterdir()}

        class NoClock(datetime):
            @classmethod
            def now(cls, tz=None):
                raise AssertionError("ambient clock read")

        with patch.object(golive_expiry, "datetime", NoClock):
            self.assertEqual(self.invoke()[0], 0)
            self.assertEqual(one([], now=NOW)["status"], "MISSING")
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.evidence.iterdir()}
        self.assertEqual(before, after)
        self.assertIs(inspect.signature(golive_expiry.runway).parameters["now"].default, inspect.Parameter.empty)
        self.assertEqual(self.evidence.stat().st_mode & 0o777, 0o700)

    def test_c4_default_clock_is_only_a_cli_fallback(self):
        """C4: without an injected now the CLI uses the UTC system clock, so a lapsed record still exits 1."""
        self.write_all(timedelta(days=10))
        stub = type("Stub", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW + 11 * timedelta(days=1))})
        with patch.object(golive_expiry, "datetime", stub):
            self.assertEqual(self.invoke(now=None)[0], 1)


if __name__ == "__main__":
    unittest.main()
