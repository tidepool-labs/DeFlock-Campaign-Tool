"""Pin the read-only input preflight for attended gate specs (C1-C4)."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from campaign_tool.records import (
    golive_gate as gate, golive_gate_inputs as inputs,
    golive_gate_specs_lanes as lanes, golive_gate_specs_s5 as s5, golive_gate_specs_s6 as s6)

NOW = datetime(2026, 11, 26, 18, tzinfo=timezone.utc)
PIN = sha256(b"synthetic pin").hexdigest()
FILE, DIR = "file", "directory"
ENGINE = "a" * 40

# Real input names per registered gate, in the order the evaluator first asks for them.
EXPECTED = {
    "s5-engine-switch": {"wrapper": FILE, "live": FILE, "disk": FILE, "evidence": DIR, "pair": FILE},
    "s5b-backup": {"quiescence": FILE, "disk": FILE, "evidence": DIR, "pair": FILE},
    "s8-units": {"evidence": DIR, "pair": FILE},
    "s6a-trust-mount": {"evidence": DIR, "run_records": FILE, "live_json": FILE, "space": FILE, "pair": FILE},
    "s6b-decisions": {"evidence": DIR, "pair": FILE},
    "s10-console": {"evidence": DIR, "pair": FILE},
    "c6-findings-on": {"evidence": DIR, "pair": FILE},
    "l17-packets": {"evidence": DIR, "pair": FILE},
    "c5-portal-apply": {"hold_release": FILE, "evidence": DIR, "pair": FILE},
    "a4-hostname": {"evidence": DIR, "pair": FILE},
}
PINS = {"s5-engine-switch": dict(wrapper=PIN, live=PIN),
        "s6a-trust-mount": dict(run_records_sha256=PIN, live_json_sha256=PIN, disk_floor_gib=1.5)}


def gate_spec(gate_id):
    pack = next(p for p in (s5, s6, lanes) if gate_id in p.GATES)
    return pack.spec(gate_id, **PINS.get(gate_id, {}))


def one(*checks):
    return {"schema": "records-golive-gate-spec-v1", "gate_id": "fixture-gate", "stages": ["3"],
            "title": "Synthetic gate", "checks": list(checks)}


class InputFixture(unittest.TestCase):
    def setUp(self):
        # A private 0700 root outside the checkout (other shards copy the repo tree); resolved so
        # no component is a symlink, which the preflight reports as SYMLINK.
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def healthy(self, gate_id, content=b"synthetic copy\n"):
        mapping = {}
        for name, kind in EXPECTED[gate_id].items():
            path = self.root / gate_id / name
            if kind == DIR:
                path.mkdir(parents=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            mapping[name] = str(path)
        return mapping

    def run_cli(self, spec, *args):
        path = self.root / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = inputs.main(["--spec", str(path), *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()


class RequiredInputTests(InputFixture):
    def test_c1_real_names_and_kinds_for_every_registered_gate(self):
        """C1: a misnamed or missed input in any registered pack changes this table."""
        registered = {g for pack in (s5, s6, lanes) for g in pack.GATES}
        self.assertEqual(registered, set(EXPECTED))
        for gate_id, names in EXPECTED.items():
            with self.subTest(gate_id=gate_id):
                got = inputs.required_inputs(gate_spec(gate_id))
                self.assertEqual({n: v["kind"] for n, v in got.items()}, names)
                self.assertEqual(list(got), list(names))

    def test_c1_used_by_lists_check_labels_in_spec_order(self):
        """C1: a shared name lists every check that asks for it, once per check, in order."""
        switch = inputs.required_inputs(gate_spec("s5-engine-switch"))
        rehearsal = "Candidate engine switch rehearsal"
        self.assertEqual(switch["wrapper"]["used_by"], ["Candidate run-records.sh", rehearsal])
        self.assertEqual(switch["pair"]["used_by"], [rehearsal])
        self.assertEqual(switch["evidence"]["used_by"], [rehearsal])
        self.assertEqual(inputs.required_inputs(gate_spec("s8-units"))["evidence"]["used_by"],
                         ["Sitting 5 engine-switch GO receipt", "Arrival rehearsal", "Monitoring rehearsal"])
        # engine_input repeated in candidate_inputs must not list its check twice.
        repeated = {"type": "evidence", "label": "r", "stage": "3", "kind": "gate", "max_age_days": 1,
                    "engine_input": "pair", "candidate_inputs": ["pair", "live"]}
        self.assertEqual(inputs.required_inputs(one(repeated))["pair"]["used_by"], ["r"])

    def test_c1_names_match_what_evaluate_asks_for(self):
        """C1: record what evaluate really looks up; a name derived differently would diverge."""
        class Recorder(dict):
            def get(self, name, default=None):
                asked.append(name)
                return name

        for gate_id in EXPECTED:
            spec = gate_spec(gate_id)
            rows = []
            for check in spec["checks"]:
                gate_name = check.get("gate_id", "")
                rows.append({"stage": check.get("stage", "3"), "kind": check.get("kind", "gate"),
                             "result": "pass", "engine": ENGINE, "recorded_at": "2026-11-26T17:00:00Z",
                             "source": {"schema": "records-rehearsal-v1", "name": gate_name + ".json",
                                        "sha256": sha256(gate_name.encode()).hexdigest()}})
            asked, kinds = [], {}
            pair = gate.Evidence(json.dumps({"engines": [ENGINE]}).encode(), PIN)

            def ledger(path):
                kinds[path] = DIR
                return gate.Ledger(rows, PIN)

            def read(path):
                kinds[path] = FILE
                return pair

            with self.subTest(gate_id=gate_id), patch.object(gate, "_read_ledger", ledger), \
                    patch.object(gate, "_read_input", read):
                gate.evaluate(spec, Recorder(), NOW)
                # Recorder hands back the name as the path, so a handler was reached for each name asked.
                self.assertEqual(set(asked), set(EXPECTED[gate_id]))
                self.assertEqual(kinds, EXPECTED[gate_id])
                self.assertEqual({n: v["kind"] for n, v in inputs.required_inputs(spec).items()},
                                 EXPECTED[gate_id])

    def test_c1_invalid_spec_and_conflicting_kind_are_refused(self):
        """C1: validation is load_spec's; one name cannot be both a file and the evidence directory."""
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            inputs.required_inputs({**gate_spec("s8-units"), "extra": 1})
        both = [{"type": "file_present", "label": "f", "input": "evidence"},
                {"type": "prior_gate", "label": "p", "gate_id": "s5-engine-switch", "max_age_days": 1}]
        with self.assertRaisesRegex(ValueError, "both a file and a directory"):
            inputs.required_inputs(one(*both))

    def test_c1_window_only_spec_needs_no_inputs(self):
        """C1: checks with no input add no name (not even the implicit evidence directory)."""
        window = {"type": "window", "label": "w", "days": ["thu"], "start": "10:00", "end": "10:10"}
        self.assertEqual(inputs.required_inputs(one(window)), {})


class CheckStateTests(InputFixture):
    PRESENT = {"type": "file_present", "label": "copy", "input": "copy"}
    LEDGER = {"type": "prior_gate", "label": "prior", "gate_id": "s5-engine-switch", "max_age_days": 1}

    def states(self, spec, **mapping):
        return {n: v["state"] for n, v in inputs.check(spec, mapping).items()}

    def test_c2_healthy_mapping_is_mapped_ok_for_every_registered_gate(self):
        """C2: the healthy control; every real gate accepts a correct mapping and flags nothing."""
        for gate_id, names in EXPECTED.items():
            with self.subTest(gate_id=gate_id):
                report = inputs.check(gate_spec(gate_id), self.healthy(gate_id))
                self.assertEqual({n: v["state"] for n, v in report.items()}, dict.fromkeys(names, "MAPPED_OK"))
                self.assertTrue(inputs.ready(report))

    def test_c2_file_states(self):
        """C2: each failure the evaluator would report as GATE_INPUT_MISSING is named distinctly."""
        spec = one(self.PRESENT)
        make = self.root.joinpath
        make("regular").write_bytes(b"x")
        make("exact").write_bytes(b"\0" * gate.MAX_INPUT)
        make("big").write_bytes(b"\0" * (gate.MAX_INPUT + 1))
        make("dir").mkdir()
        make("link").symlink_to(make("regular"))
        make("fifo")
        os.mkfifo(make("fifo"))
        make("real_dir").mkdir()
        make("real_dir", "inside").write_bytes(b"x")
        make("linked_dir").symlink_to(make("real_dir"))
        cases = {"regular": "MAPPED_OK", "exact": "MAPPED_OK", "big": "TOO_LARGE", "dir": "WRONG_KIND",
                 "fifo": "WRONG_KIND", "link": "SYMLINK", "gone": "MISSING_PATH",
                 "regular/under": "MISSING_PATH", "linked_dir/inside": "SYMLINK"}
        for name, state in cases.items():
            with self.subTest(path=name):
                self.assertEqual(self.states(spec, copy=str(make(name))), {"copy": state})
        self.assertEqual(self.states(spec), {"copy": "NOT_MAPPED"})
        self.assertEqual(self.states(spec, copy=None), {"copy": "NOT_MAPPED"})
        self.assertEqual(self.states(spec, copy=12), {"copy": "MISSING_PATH"})
        self.assertEqual(self.states(spec, copy="bad\0path"), {"copy": "MISSING_PATH"})

    def test_c2_directory_states(self):
        """C2: the evidence directory must be a real directory, not a file or a link to one."""
        spec = one(self.LEDGER)
        make = self.root.joinpath
        make("ledger").mkdir()
        make("file").write_bytes(b"x")
        make("link").symlink_to(make("ledger"))
        cases = {"ledger": "MAPPED_OK", "file": "WRONG_KIND", "link": "SYMLINK", "gone": "MISSING_PATH"}
        for name, state in cases.items():
            with self.subTest(path=name):
                self.assertEqual(self.states(spec, evidence=str(make(name))), {"evidence": state})
        self.assertEqual(self.states(spec), {"evidence": "NOT_MAPPED"})

    def test_c2_typo_is_unused_with_a_hint_and_the_real_name_stays_unmapped(self):
        """C2: `wraper=` must not vanish silently; without it the operator sees only a NO-GO."""
        mapping = self.healthy("s5-engine-switch")
        mapping["wraper"] = mapping.pop("wrapper")
        report = inputs.check(gate_spec("s5-engine-switch"), mapping)
        self.assertEqual(report["wrapper"]["state"], "NOT_MAPPED")
        self.assertEqual(report["wraper"], {"state": "UNUSED", "hint": "wrapper"})
        self.assertFalse(inputs.ready(report))
        # The same typo on the evaluator: silently ignored, only a missing-input NO-GO remains.
        receipt = gate.evaluate(gate_spec("s5-engine-switch"), mapping, NOW)
        self.assertEqual(receipt["checks"][0]["code"], "GATE_INPUT_MISSING")

    def test_c2_unused_without_a_close_name_has_no_hint(self):
        """C2: a far-off extra name is still reported, but never gets a misleading suggestion."""
        report = inputs.check(one(self.PRESENT), {"copy": str(self.root), "zzzzzz": "x"})
        self.assertEqual(report["zzzzzz"], {"state": "UNUSED", "hint": None})

    def test_c2_nothing_is_read_or_written(self):
        """C2/C4: lstat only; opening, hashing or evaluating any input would trip a patch here."""
        mapping = self.healthy("s5-engine-switch", content=b"SECRET-CONTENT")
        mapping["extra"] = str(self.root / "gone")
        paths = sorted(self.root.rglob("*"))
        # atime is not compared: listing a directory (as this test does) bumps it under relatime.
        before = [(p, p.lstat().st_mtime_ns, p.lstat().st_size) for p in paths]

        def refuse(*args, **kwargs):
            raise AssertionError("input was opened or evaluated")

        with patch("builtins.open", refuse), patch("os.open", refuse), patch.object(gate, "evaluate", refuse), \
                patch.object(gate, "_read_input", refuse), patch.object(gate, "_read_ledger", refuse):
            report = inputs.check(gate_spec("s5-engine-switch"), mapping)
        self.assertEqual(report["extra"]["state"], "UNUSED")
        self.assertEqual(sorted(self.root.rglob("*")), paths)
        self.assertEqual([(p, p.lstat().st_mtime_ns, p.lstat().st_size) for p in paths], before)


class CliTests(InputFixture):
    def test_c3_ready_run_exits_zero_with_one_line_per_required_input(self):
        """C3: the healthy control; every name mapped, nothing unused."""
        gate_id = "s5-engine-switch"
        mapping = self.healthy(gate_id)
        code, out, err = self.run_cli(gate_spec(gate_id), *(f"--input={n}={p}" for n, p in mapping.items()))
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line.split()[0] for line in lines], list(EXPECTED[gate_id]))
        self.assertTrue(all("MAPPED_OK" in line for line in lines))
        self.assertIn('wrapper file: MAPPED_OK (used by "Candidate run-records.sh", '
                      '"Candidate engine switch rehearsal")', lines)

    def test_c3_typo_exits_one_and_names_the_unused_input_and_hint(self):
        """C3: the CONTEXT typo; the operator learns of it before the sitting, not from a NO-GO."""
        gate_id = "s5-engine-switch"
        mapping = self.healthy(gate_id)
        mapping["wraper"] = mapping.pop("wrapper")
        code, out, err = self.run_cli(gate_spec(gate_id), *(f"--input={n}={p}" for n, p in mapping.items()))
        self.assertEqual((code, err), (1, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), len(EXPECTED[gate_id]) + 1)
        self.assertIn("wrapper file: NOT_MAPPED", out)
        self.assertIn("wraper unused: UNUSED (closest required name: wrapper)", lines)

    def test_c3_any_unready_state_exits_one_and_json_matches(self):
        """C3: --json carries the same states; a missing path alone is enough for exit 1."""
        spec = one(CheckStateTests.PRESENT)
        code, out, _ = self.run_cli(spec, "--input", f"copy={self.root / 'gone'}", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"gate_id": "fixture-gate", "ready": False, "inputs": {
            "copy": {"state": "MISSING_PATH", "kind": FILE, "used_by": ["copy"]}}})
        good = self.root / "good"
        good.write_bytes(b"x")
        code, out, _ = self.run_cli(spec, "--input", f"copy={good}", "--json")
        self.assertEqual((code, json.loads(out)["ready"]), (0, True))
        # Every required input healthy but one extra name: still exit 1, because UNUSED is never ready.
        code, out, _ = self.run_cli(spec, "--input", f"copy={good}", "--input", f"copyy={good}", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["inputs"]["copyy"], {"state": "UNUSED", "hint": "copy"})
        self.assertEqual(json.loads(out)["inputs"]["copy"]["state"], "MAPPED_OK")

    def test_c3_bad_spec_or_malformed_input_exits_two(self):
        """C3: same --input rule as golive_gate, including duplicates; a bad spec is not a NO-GO."""
        spec = gate_spec("s8-units")
        for args in (("--input", "noequals"), ("--input", "=path"), ("--input", "name="),
                     ("--input", "a=1", "--input", "a=2")):
            with self.subTest(args=args):
                code, out, err = self.run_cli(spec, *args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--input requires a distinct NAME=PATH", err)
        code, out, err = self.run_cli({**spec, "schema": "other"})
        self.assertEqual((code, out), (2, ""))
        self.assertIn("schema", err)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            inputs.main(["--spec", str(self.root / "absent.json")])
        self.assertEqual(raised.exception.code, 2)

    def test_c4_error_diagnostics_carry_no_path(self):
        """C4: an unreadable spec or a path-bearing --input error still exits 2 without echoing any path.

        Believable bug: passing str(OSError) to parser.error prints the full spec path.
        """
        marker = "private-marker"
        missing = self.root / marker / "spec.json"
        (self.root / "adir.json").mkdir()
        spec_file = self.root / "bad.json"
        spec_file.write_text("{not json", encoding="utf-8")
        for spec_path in (missing, self.root / "adir.json", spec_file):
            with self.subTest(spec=spec_path.name):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                    inputs.main(["--spec", str(spec_path)])
                self.assertEqual((raised.exception.code, out.getvalue()), (2, ""))
                self.assertIn("error: ", err.getvalue())
                for leaked in (str(self.root), marker, str(spec_path)):
                    self.assertNotIn(leaked, err.getvalue())
        # A path inside a ValueError message (here from a mapped --input) is scrubbed too.
        leaky = self.root / marker / "copy.bin"
        with patch.object(inputs, "required_inputs", side_effect=ValueError(f"cannot use {leaky}")):
            code, out, err = self.run_cli(gate_spec("s8-units"), "--input", f"a={leaky}")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot use <path>", err)
        self.assertNotIn(marker, err)

    def test_c4_option_typo_and_other_parse_errors_echo_no_value(self):
        """RETURN t_455fcda9 (1): argparse quoted the unrecognized argument, path included."""
        marker = "private-marker"
        leaky = f"copy={self.root / marker / 'copy.bin'}"
        spec = gate_spec("s8-units")
        for args in (("--imput", leaky), (f"--imput={leaky}",), ("--input", leaky, "--jsn"),
                     ("--input",), (leaky,)):
            with self.subTest(args=args):
                code, out, err = self.run_cli(spec, *args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error: invalid command line", err)
                for leaked in (marker, str(self.root), "copy.bin"):
                    self.assertNotIn(leaked, err)
        # Missing --spec still exits 2 and says nothing path-like either.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as raised:
            inputs.main(["--input", leaky])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn(marker, err.getvalue())
        # The earlier sanitized diagnostics keep their own text (missing-spec protection preserved).
        code, out, err = self.run_cli(spec, "--input", "noequals")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--input requires a distinct NAME=PATH", err)

    def test_c3_control_characters_in_names_and_hints_stay_on_one_line(self):
        """RETURN t_455fcda9 (2): one text line per input even for names with line breaks."""
        good = self.root / "good"
        good.write_bytes(b"x")
        odd = "co\npy"
        spec = one({**CheckStateTests.PRESENT, "input": odd, "label": "odd"})
        for extra, hint_name in (("cop\ry", odd), ("zz\u2028top", None), ("tab\tname", None)):
            with self.subTest(extra=extra):
                code, out, err = self.run_cli(spec, "--input", f"{odd}={good}", "--input", f"{extra}={good}")
                self.assertEqual((code, err), (1, ""))
                lines = out.split("\n")
                self.assertEqual(lines[-1], "")
                self.assertEqual(len(lines[:-1]), 2, out)
                self.assertEqual(lines[0], 'co\\npy file: MAPPED_OK (used by "odd")')
                self.assertTrue(lines[1].startswith(ascii(extra)[1:-1] + " unused: UNUSED"), lines[1])
                if hint_name:
                    self.assertTrue(lines[1].endswith("(closest required name: co\\npy)"), lines[1])
                self.assertTrue(all(ch.isprintable() for line in lines for ch in line))
                # JSON keeps the exact values; matching is unchanged.
                code, out, _ = self.run_cli(spec, "--input", f"{odd}={good}", "--input", f"{extra}={good}", "--json")
                report = json.loads(out)["inputs"]
                self.assertEqual(report[odd]["state"], "MAPPED_OK")
                self.assertEqual(report[extra], {"state": "UNUSED", "hint": hint_name})
        # Positive control: an ordinary name prints unchanged.
        code, out, err = self.run_cli(one(CheckStateTests.PRESENT), "--input", f"copy={good}")
        self.assertEqual((code, out, err), (0, 'copy file: MAPPED_OK (used by "copy")\n', ""))

    def test_c4_output_names_and_states_only(self):
        """C4: neither contents, digests nor paths reach stdout or stderr, in text or JSON form."""
        gate_id = "s6a-trust-mount"
        content = b"SECRET-CONTENT-MARKER"
        mapping = self.healthy(gate_id, content=content)
        args = [f"--input={n}={p}" for n, p in mapping.items()]
        for extra in ((), ("--json",)):
            code, out, err = self.run_cli(gate_spec(gate_id), *args, *extra)
            self.assertEqual(code, 0)
            for leaked in (content.decode(), sha256(content).hexdigest(), str(self.root), PIN):
                self.assertNotIn(leaked, out + err)


if __name__ == "__main__":
    unittest.main()
