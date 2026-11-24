import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_adapt_pair as adapter, golive_evidence, golive_ledger, golive_pair

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
OBSERVED = "2026-01-02T10:00:00Z"
WRAPPER, LIVE, OTHER = "a" * 64, "b" * 64, "c" * 64
ENGINE = "1234567890abcdef" * 2 + "12345678"  # 40 hex
OTHER_ENGINE = "f" * 40
REFUSED = (2, "", "PAIR_EVIDENCE_BAD_INPUT\n")


def report(**changes):
    value = {"wrapper_sha256": WRAPPER, "live_sha256": LIVE, "engines": [f"engine-{ENGINE}"],
             "codes": ["PAIR_MATCH"]}
    value.update(changes)
    return value


def expected(**changes):
    value = {"wrapper_sha256": WRAPPER, "live_sha256": LIVE, "engine": ENGINE, "observed_at": OBSERVED}
    value.update(changes)
    return value


def row(result="pass", codes=("PAIR_MATCH",), *, name="pair.json", raw=None, payload=None):
    if raw is None:
        raw = json.dumps(payload or report(), sort_keys=True, separators=(",", ":")).encode()
    return {"schema": "records-golive-evidence-v1", "stage": "15", "kind": "live", "result": result,
            "recorded_at": OBSERVED, "engine": ENGINE, "actor": "operator", "codes": list(codes),
            "source": {"schema": "records-golive-pair-report-v1", "sha256": hashlib.sha256(raw).hexdigest(),
                       "name": name}}


class PairRecordTests(unittest.TestCase):
    def test_c1_c2_matching_pins_pass(self):
        """C1/C2: matching pins give one valid stage-15 pass row. Fails if the stage, kind, actor,
        engine or source schema is wrong, or if the row is not schema-valid."""
        result = adapter.records(report(), expected(), NOW)
        self.assertEqual(result, row())
        self.assertIsNone(golive_evidence.validate(result))

    def test_c2_each_drift_code_fails(self):
        """C2: every compare code gives a fail row carrying exactly the compare codes. Fails if a
        drift is ignored, a code is dropped, or the pass/fail test is not driven by compare."""
        cases = (
            (report(wrapper_sha256=OTHER), ["PAIR_WRAPPER_DRIFT"]),
            (report(live_sha256=OTHER), ["PAIR_LIVE_DRIFT"]),
            (report(wrapper_sha256=OTHER, live_sha256=OTHER), ["PAIR_WRAPPER_DRIFT", "PAIR_LIVE_DRIFT"]),
            (report(engines=[f"engine-{OTHER_ENGINE}"]), ["PAIR_ENGINE_DRIFT"]),
            (report(engines=[f"engine-{ENGINE}", f"engine-{OTHER_ENGINE}"]), ["PAIR_ENGINE_MIXED"]),
            (report(engines=[]), ["PAIR_ENGINE_MIXED"]),
            (report(engines=["engine-1234567"]), ["PAIR_ENGINE_DRIFT"]),  # a 7-char alias is not the pin
        )
        for index, (payload, codes) in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(adapter.records(payload, expected(), NOW), row("fail", codes, payload=payload))

    def test_c2_report_codes_are_not_trusted(self):
        """C2: a saved report that claims PAIR_MATCH over drifted bytes still fails. Fails if the
        adapter copies the report's own codes instead of recomputing compare against the pins."""
        payload = report(live_sha256=OTHER, codes=["PAIR_MATCH"])
        self.assertEqual(adapter.records(payload, expected(), NOW)["codes"], ["PAIR_LIVE_DRIFT"])
        self.assertEqual(adapter.records(report(codes=["PAIR_ENGINE_DRIFT"]), expected(), NOW)["result"], "pass")

    def test_c3_bad_report_shapes_are_refused(self):
        """C3: malformed reports raise ValueError, not a row. Fails if a short pin, a stray field
        or a non-list engines value slips through to a pass."""
        cases = ([], report(extra=1), {k: v for k, v in report().items() if k != "codes"},
                 report(wrapper_sha256="A" * 64), report(live_sha256=LIVE[:-1]), report(engines="engine-" + ENGINE),
                 report(engines=["engine-XYZ"]), report(engines=[7]), report(codes="PAIR_MATCH"), report(codes=[None]))
        for index, payload in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                adapter.records(payload, expected(), NOW)

    def test_c3_bad_pins_and_clocks_are_refused(self):
        """C3: malformed pins and out-of-window times raise. Fails if a 7-char or engine-prefixed pin,
        uppercase hex, a future stamp, a stamp over 24 hours old or a naive clock is accepted."""
        bad = (expected(engine=ENGINE[:7]), expected(engine=f"engine-{ENGINE}"), expected(engine=ENGINE.upper()),
               expected(wrapper_sha256=WRAPPER[:-1]), expected(live_sha256=None), expected(observed_at=None),
               expected(observed_at="2026-01-02T12:00:01Z"), expected(observed_at="2026-01-01T11:59:59Z"),
               expected(observed_at="2026-01-02T10:00:00+00:00"), expected(observed_at="2026-1-2T10:00:00Z"),
               {k: v for k, v in expected().items() if k != "engine"}, None)
        for index, value in enumerate(bad):
            with self.subTest(case=index), self.assertRaises(ValueError):
                adapter.records(report(), value, NOW)
        with self.assertRaises(ValueError):
            adapter.records(report(), expected(), NOW.replace(tzinfo=None))

    def test_c3_window_edges_are_accepted(self):
        """C3: exactly now and exactly 24 hours old are inside the window. Fails on an off-by-one
        that refuses the boundary."""
        for stamp in ("2026-01-02T12:00:00Z", (NOW - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")):
            with self.subTest(stamp=stamp):
                self.assertEqual(adapter.records(report(), expected(observed_at=stamp), NOW)["recorded_at"], stamp)


class PairCLITests(unittest.TestCase):
    def invoke(self, path, *args):
        out, err = io.StringIO(), io.StringIO()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(NOW.isoformat()).astimezone(tz)

        with redirect_stdout(out), redirect_stderr(err), patch.object(adapter, "datetime", Clock):
            status = adapter.main([str(path), *args])
        return status, out.getvalue(), err.getvalue()

    def pins(self, **changes):
        values = {"--expect-wrapper": WRAPPER, "--expect-live": LIVE, "--engine": ENGINE, "--observed-at": OBSERVED}
        values.update(changes)
        return [item for pair in values.items() if pair[1] is not None for item in pair]

    def test_c1_c2_json_binds_saved_bytes(self):
        """C1/C2: --json prints a one-row list whose source hash is of the saved bytes and name is the
        file basename. Fails if the hash is of re-serialised JSON or the row is not wrapped in a list."""
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / "capture.json"
            raw = (json.dumps(report(), indent=2) + "\n").encode()
            path.write_bytes(raw)
            status, out, err = self.invoke(path, *self.pins(), "--json")
            self.assertEqual((status, json.loads(out), err), (0, [row(name="capture.json", raw=raw)], ""))
            text = f"15\tlive\tpass\t{OBSERVED}\trecords-golive-pair-report-v1\tPAIR_MATCH\n"
            self.assertEqual(self.invoke(path, *self.pins()), (0, text, ""))
            self.assertEqual(path.read_bytes(), raw)

    def test_c2_fail_row_is_printed_with_status_1(self):
        """C2/C3: a drifted pair prints its fail row and exits 1, distinct from refusal (2). Fails if a
        mismatch is refused as bad input or exits 0."""
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / "drift.json"
            payload = report(engines=[f"engine-{ENGINE}", f"engine-{OTHER_ENGINE}"], wrapper_sha256=OTHER)
            raw = json.dumps(payload).encode()
            path.write_bytes(raw)
            status, out, err = self.invoke(path, *self.pins(), "--json")
            self.assertEqual((status, err), (1, ""))
            self.assertEqual(json.loads(out), [row("fail", ["PAIR_WRAPPER_DRIFT", "PAIR_ENGINE_MIXED"],
                                                   name="drift.json", raw=raw)])

    def test_c3_bad_files_are_refused_without_echo(self):
        """C3: symlink, directory, FIFO, missing, oversize, duplicate-field and malformed files give the
        single code, no stdout, and no echo of the path or content. Fails if any is read or echoed."""
        secret = "SECRET-CANARY"
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            root = Path(tmp)
            good = root / "good.json"
            good.write_text(json.dumps(report()))
            link, fifo = root / f"{secret}-link.json", root / f"{secret}-fifo.json"
            link.symlink_to(good)
            os.mkfifo(fifo)
            cases = [link, fifo, root, root / f"{secret}-missing.json"]
            for name, raw in (("dup", b'{"engines":[],"engines":[]}'), ("trunc", b'{"x": "' + secret.encode()),
                              ("big", b" " * (adapter.MAX_BYTES + 1)), ("bin", b"\xff" + secret.encode()),
                              ("nest", b"[" * 30000), ("list", b"[]")):
                cases.append(root / f"{secret}-{name}.json")
                cases[-1].write_bytes(raw)
            dup = json.dumps(report()).replace("{", '{"live_sha256": "' + LIVE + '",', 1).encode()
            (root / "dupfield.json").write_bytes(dup)
            cases.append(root / "dupfield.json")
            for path in cases:
                with self.subTest(path=path.name):
                    result = self.invoke(path, *self.pins(), "--json")
                    self.assertEqual(result, REFUSED)
                    self.assertNotIn(secret, "".join(result[1:]))

    def test_c3_bad_pins_and_stamps_are_refused_without_echo(self):
        """C3: missing/malformed pins (incl. a 7-char engine) and future/stale stamps are refused. Fails if
        a pin is optional, a short engine is accepted, or argparse echoes the offending value."""
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / "pair.json"
            path.write_text(json.dumps(report()))
            for flag in ("--expect-wrapper", "--expect-live", "--engine", "--observed-at"):
                with self.subTest(missing=flag):
                    self.assertEqual(self.invoke(path, *self.pins(**{flag: None})), REFUSED)
            for changes in ({"--engine": ENGINE[:7]}, {"--engine": f"engine-{ENGINE}"}, {"--expect-wrapper": "A" * 64},
                            {"--expect-live": "b" * 63}, {"--observed-at": "2026-01-02T12:00:01Z"},
                            {"--observed-at": "2026-01-01T11:59:59Z"}, {"--observed-at": "yesterday"}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.invoke(path, *self.pins(**changes)), REFUSED)
            for extra in (["--bogus", "SECRET-CANARY"], ["--engine"], ["--eng", ENGINE]):
                with self.subTest(extra=extra):
                    result = self.invoke(path, *self.pins(), *extra)
                    self.assertEqual(result, REFUSED)
                    self.assertNotIn("SECRET-CANARY", "".join(result[1:]))

    def test_c4_reads_files_only(self):
        """C4: the adapter does not run golive_pair, spawn processes or write anywhere. Fails if it calls
        golive_pair.inspect, shells out, or leaves a new file or changed bytes beside the input."""
        self.assertFalse(hasattr(adapter, "subprocess"))
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / "pair.json"
            path.write_text(json.dumps(report()))
            before = (sorted(os.listdir(tmp)), path.read_bytes())
            boom = AssertionError("must not execute anything")
            with patch.object(golive_pair, "inspect", side_effect=boom), patch.object(golive_pair, "main", side_effect=boom), \
                    patch.object(subprocess, "run", side_effect=boom), patch.object(subprocess, "Popen", side_effect=boom), \
                    patch.object(os, "system", side_effect=boom):
                self.assertEqual(self.invoke(path, *self.pins(), "--json")[0], 0)
            self.assertEqual((sorted(os.listdir(tmp)), path.read_bytes()), before)


class PairLedgerTests(unittest.TestCase):
    def stage15(self, evidence):
        rows = golive_ledger.build([], evidence, NOW)
        return next(item for item in rows if item["stage"] == "15")

    def test_d_pass_row_makes_stage_15_live(self):
        """(d): the ledger accepts the pass row and reads stage 15 LIVE. Fails if the row is rejected,
        mis-staged or leaves the stage BUILT, which keeps M4 unreachable."""
        result = adapter.records(report(), expected(), NOW)
        before = self.stage15([])
        self.assertNotEqual(before["state"], "LIVE")
        after = self.stage15([result])
        self.assertEqual((after["state"], after["live"]), ("LIVE", {"status": "evidence", "recorded_at": OBSERVED}))

    def test_d_fail_row_does_not_make_stage_15_live(self):
        """(d): a drift row blocks stage 15 instead of proving it. Fails if a fail row is read as proof."""
        for payload in (report(wrapper_sha256=OTHER), report(engines=[f"engine-{OTHER_ENGINE}"])):
            with self.subTest(payload=payload):
                cell = self.stage15([adapter.records(payload, expected(), NOW)])
                self.assertEqual((cell["state"], cell["live"]["reason"]), ("BLOCKED", "LIVE_FAILED"))

    def test_d_row_round_trips_through_the_evidence_reader(self):
        """(d): the row, stored under its reader-bound filename, loads back unchanged. Fails if the row is
        valid in memory yet rejected by golive_evidence.load (filename or schema binding)."""
        result = adapter.records(report(), expected(), NOW)
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = Path(tmp) / golive_evidence._filename(result)
            path.write_text(json.dumps(result))
            path.chmod(0o600)
            self.assertEqual(golive_evidence.load(tmp), [result])


if __name__ == "__main__":
    unittest.main()
