"""Offline tests for the plain-words gate receipt explanation (contracts C1-C4)."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

from campaign_tool.records import golive_gate as gate
from campaign_tool.records import golive_gate_explain as explain_mod
from campaign_tool.records.golive_gate_explain import EXPLANATIONS, explain
from tests.records.test_golive_gate import DISK, NOW, PIN, PRESENT, SHA, WINDOW, GateFixture, spec

EVIDENCE = {"type": "evidence", "label": "rehearsal", "stage": "3", "kind": "rehearsal", "max_age_days": 2}
PINNED = dict(EVIDENCE, engine_input="pair")
ENGINE = "a" * 40
LIVE_WORDS = re.compile(r"\b(run|switch|approve|install|release|deploy|enable|start|apply|publish|send|restart)\b", re.I)


class ExplainFixture(GateFixture):
    def setUp(self):
        super().setUp()
        self.root.chmod(0o700)
        self.rows = 0

    def ledger(self, *rows):
        """A private ledger directory holding (age_days, outcome, engine) rehearsal rows."""
        folder = self.root / f"ledger{len(list(self.root.glob('ledger*')))}"
        folder.mkdir()
        for age, outcome, engine in rows:
            self.rows += 1
            when = NOW - timedelta(days=age)
            digest = hashlib.sha256(str(self.rows).encode()).hexdigest()
            record = {"schema": "records-golive-evidence-v1", "stage": "3", "kind": "rehearsal",
                      "result": outcome, "engine": engine, "recorded_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "source": {"schema": "records-rehearsal-v1", "name": "r.json", "sha256": digest},
                      "actor": "rehearsal", "codes": []}
            path = folder / f"{when:%Y%m%dT%H%M%SZ}-3-rehearsal-{digest[:8]}.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            path.chmod(0o600)
        return folder

    def produced(self):
        """Receipts made by golive_gate.evaluate on synthetic inputs, keyed by what they show."""
        copy = self.fixture("copy", b"synthetic copy\n")
        other = self.fixture("other", b"changed copy\n")
        good = self.fixture("good-df", b"  Avail\n 1610612736\n")
        low = self.fixture("low-df", b"  Avail\n 1024\n")
        junk = self.fixture("junk-df", b"no space reading here\n")
        pair = self.fixture("pair", json.dumps({"engines": ["engine-" + "b" * 40]}).encode())
        full = spec(SHA, DISK, WINDOW, PRESENT)
        later = NOW + timedelta(days=1)  # Sunday: the Saturday window is closed
        evidence = {"evidence": self.ledger((1, "pass", ENGINE))}
        cases = {
            "go": (full, {"copy": copy, "space": good}, NOW),
            "missing": (spec(SHA, DISK, PRESENT), {}, NOW),
            "mismatch": (spec(SHA), {"copy": other}, NOW),
            "unparsed": (spec(DISK), {"space": junk}, NOW),
            "below": (spec(DISK), {"space": low}, NOW),
            "closed": (spec(WINDOW), {}, later),
            "ev_missing": (spec(EVIDENCE), {"evidence": self.ledger()}, NOW),
            "ev_failed": (spec(EVIDENCE), {"evidence": self.ledger((1, "fail", ENGINE))}, NOW),
            "ev_stale": (spec(EVIDENCE), {"evidence": self.ledger((3, "pass", ENGINE))}, NOW),
            "ev_unreadable": (spec(EVIDENCE), {"evidence": self.root / "no-such-folder"}, NOW),
            "ev_match": (spec(EVIDENCE), evidence, NOW),
            "ev_engine": (spec(PINNED), {**evidence, "pair": pair}, NOW),
        }
        return {name: gate.evaluate(value, inputs, when) for name, (value, inputs, when) in cases.items()}

    def write(self, name, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        path = self.fixture(name, raw)
        path.chmod(0o600)
        return path


class ExplainTests(ExplainFixture):
    def test_c1_go_receipt_prints_go_and_no_fix(self):
        """C1: a GO receipt names the gate and stages and never invents a fix (bug: fix on every line)."""
        lines = explain(self.produced()["go"])
        self.assertEqual(lines[0], "GO: gate fixture-gate (stages 3, A1)")
        self.assertEqual([line.split(":")[1].split(" - ")[0] for line in lines[1:-1]], [" pass"] * 4)
        self.assertIn('pin: pass - the copied file matches the pinned SHA-256', lines)
        self.assertFalse([line for line in lines if line.startswith("fix:")])

    def test_c1_every_non_pass_check_gets_one_line_and_one_fix(self):
        """C1: each fail or unknown check is `label: state - sentence` followed by its own fix: line."""
        for name, receipt in self.produced().items():
            lines = explain(receipt)
            self.assertEqual(lines[0].split(":")[0], receipt["verdict"], name)
            body = lines[1:-1]
            expected = []
            for check in receipt["checks"]:
                _, meaning, fix = EXPLANATIONS[check["code"]]
                expected.append(f"{check['label']}: {check['state']} - {meaning}")
                if check["state"] != "pass":
                    expected.append(f"fix: {fix}")
            self.assertEqual(body, expected, name)

    def test_c1_verdict_always_equals_receipts_and_inconsistency_is_refused(self):
        """C1: a GO receipt hiding a failure, or NO-GO with all passes, is refused, not printed."""
        receipts = self.produced()
        for name, receipt in receipts.items():
            flipped = dict(receipt, verdict="NO-GO" if receipt["verdict"] == "GO" else "GO")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "inconsistent gate verdict"):
                explain(flipped)
        good, bad = receipts["go"], receipts["mismatch"]
        mangled = {
            "extra key": dict(good, note="x"), "missing key": {k: v for k, v in good.items() if k != "inputs"},
            "schema": dict(good, schema="records-golive-gate-v2"), "gate id": dict(good, gate_id="Bad Id"),
            "stages": dict(good, stages=[]), "stage type": dict(good, stages=[3]),
            "no checks": dict(good, checks=[]), "not a list": dict(good, checks={}),
            "stamp": dict(good, evaluated_at="yesterday"), "inputs": dict(good, inputs=[]),
            "digest": dict(good, inputs={"copy": "abc"}), "input name": dict(good, inputs={"a=b": PIN}),
            "check key": dict(good, checks=[dict(good["checks"][0], extra=1)]),
            "state": dict(good, checks=[dict(good["checks"][0], state="maybe")]),
            "code shape": dict(good, checks=[dict(good["checks"][0], code="bad code")]),
            "blank label": dict(good, checks=[dict(good["checks"][0], label=" ")]),
            "state fits code": dict(bad, verdict="GO", checks=[dict(bad["checks"][0], state="pass")]),
            "not an object": [good], "none": None,
        }
        for name, value in mangled.items():
            with self.subTest(name=name), self.assertRaises(ValueError) as raised:
                explain(value)
            self.assertIs(type(raised.exception), ValueError)

    def test_c2_table_covers_exactly_the_codes_in_golive_gate_source(self):
        """C2: a code added to golive_gate.py without text here fails (bug: new code prints as unrecognized)."""
        source = Path(gate.__file__).read_text(encoding="utf-8")
        found = set(re.findall(r'"((?:GATE|EVIDENCE)_[A-Z0-9_]+)"', source))
        self.assertEqual(len(found), 15)
        self.assertEqual(set(EXPLANATIONS), found)
        self.assertEqual(explain_mod.RECEIPT_SCHEMA, gate.RECEIPT_SCHEMA)

    def test_c2_every_code_is_produced_offline_and_has_its_own_sentence(self):
        """C2: gate.evaluate yields all 15 codes with state matching the table, each with distinct text."""
        seen = {(check["code"], check["state"]) for receipt in self.produced().values() for check in receipt["checks"]}
        self.assertEqual(seen, {(code, entry[0]) for code, entry in EXPLANATIONS.items()})
        self.assertEqual(len({entry[1] for entry in EXPLANATIONS.values()}), len(EXPLANATIONS))
        for code, (state, meaning, fix) in EXPLANATIONS.items():
            self.assertEqual(fix is None, state == "pass", code)

    def test_c2_unknown_code_is_named_and_never_raises(self):
        """C2: a code from a newer gate is shown as unrecognized with a generic fix, not a crash."""
        receipt = dict(self.produced()["mismatch"])
        receipt["checks"] = [dict(receipt["checks"][0], code="GATE_NEW_THING"),
                             dict(receipt["checks"][0], label="ok", state="pass", code="GATE_NEWER")]
        lines = explain(receipt)
        self.assertEqual(lines[1:4], ["pin: fail - unrecognized code GATE_NEW_THING",
                                      "fix: " + explain_mod.UNKNOWN_FIX, "ok: pass - unrecognized code GATE_NEWER"])

    def test_c3_fix_lines_are_offline_and_nothing_prints_digests_or_paths(self):
        """C3: no fix tells anyone to do something live; no input digest or path reaches the output."""
        for name, receipt in self.produced().items():
            text = "\n".join(explain(receipt))
            with self.subTest(name=name):
                for line in text.splitlines():
                    if line.startswith("fix:"):
                        self.assertIsNone(LIVE_WORDS.search(line), line)
                for digest in receipt["inputs"].values():
                    self.assertNotIn(digest, text)
                self.assertNotIn(str(self.root), text)
                self.assertNotIn("/", text)
        for _, _, fix in EXPLANATIONS.values():
            self.assertTrue(fix is None or LIVE_WORDS.search(fix) is None)

    def test_c3_labels_cannot_smuggle_digests_paths_or_control_text(self):
        """C3: a label naming a digest, path or escape code is redacted before printing."""
        receipt = dict(self.produced()["mismatch"])
        label = f"copy {PIN} at {self.root}/file \x1b[31mred\x07"
        receipt["checks"] = [dict(receipt["checks"][0], label=label)]
        line = explain(receipt)[1]
        self.assertTrue(line.startswith("copy [digest] at [path] [31mred: fail - "), line)
        self.assertNotIn("\x1b", line)

    def test_c1_timestamp_must_be_a_real_utc_instant_not_in_the_future(self):
        """C1: the recorder rejects impossible and future times, so must the explainer (bug: shape-only check)."""
        good = self.produced()["go"]
        for stamp in ("2025-99-99T99:99:99Z", "2026-02-30T00:00:00Z", "2026-11-21T24:00:00Z", "2026-11-21T20:00:60+00:00"):
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ValueError, "invalid gate timestamp"):
                explain(dict(good, evaluated_at=stamp))
        self.assertTrue(explain(dict(good, evaluated_at="2026-11-21T20:00:00.123456+00:00"))[0].startswith("GO:"))
        with self.assertRaisesRegex(ValueError, "in the future"):
            explain(dict(good, evaluated_at="2999-01-01T00:00:00Z"))
        self.assertTrue(explain(good, now=NOW)[0].startswith("GO:"))
        with self.assertRaisesRegex(ValueError, "in the future"):
            explain(good, now=NOW - timedelta(seconds=1))

    def test_c3_unknown_code_and_gate_id_cannot_carry_a_digest_in_text_or_json(self):
        """C3: a digest hidden in an unrecognized code or the gate id is redacted in both formats (bug: echoed)."""
        base = self.produced()["mismatch"]
        upper, lower = PIN.upper(), PIN
        receipt = dict(base, gate_id=f"gate-{lower}", checks=[dict(base["checks"][0], code="X" + upper)])
        text = "\n".join(explain(receipt))
        self.assertNotIn(upper, text)
        self.assertNotIn(lower, text)
        self.assertIn("unrecognized code X[digest]", text)
        self.assertTrue(text.startswith("NO-GO: gate gate-[digest] (stages"), text)
        path = self.write("digest.json", receipt)
        out = io.StringIO()
        with redirect_stdout(out):
            code = explain_mod.main([str(path), "--json"])
        out = out.getvalue()
        parsed = json.loads(out)
        self.assertEqual((code, parsed["gate_id"], parsed["checks"][0]["code"]), (1, "gate-[digest]", "X[digest]"))
        self.assertNotIn(upper, out)
        self.assertNotIn(lower, out)


class ExplainCliTests(ExplainFixture):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = explain_mod.main([str(arg) for arg in argv])
        return code, out.getvalue(), err.getvalue()

    def test_c4_exit_codes_follow_the_receipts_verdict_and_nothing_is_written(self):
        """C4: GO exits 0 and NO-GO exits 1; the receipt and its folder are untouched."""
        receipts = self.produced()
        go, no_go = self.write("go.json", receipts["go"]), self.write("nogo.json", receipts["mismatch"])
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir() if p.is_file()}
        names = set(os.listdir(self.root))
        self.assertEqual(self.run_cli(go), (0, "\n".join(explain(receipts["go"])) + "\n", ""))
        code, out, err = self.run_cli(no_go)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out.splitlines()[:3], explain(receipts["mismatch"])[:3])
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir() if p.is_file()}, before)
        self.assertEqual(set(os.listdir(self.root)), names)

    def test_c4_json_matches_text_and_works_as_a_module(self):
        """C4: --json carries the same verdict, meanings and fixes; `python -m` gives the same exit status."""
        receipt = self.produced()["mismatch"]
        path = self.write("nogo.json", receipt)
        code, out, _ = self.run_cli(path, "--json")
        parsed = json.loads(out)
        self.assertEqual((code, parsed["verdict"], parsed["gate_id"], parsed["stages"]), (1, "NO-GO", "fixture-gate", ["3", "A1"]))
        _, meaning, fix = EXPLANATIONS["GATE_SHA256_MISMATCH"]
        self.assertEqual(parsed["checks"], [{"label": "pin", "state": "fail", "code": "GATE_SHA256_MISMATCH",
                                             "meaning": meaning, "fix": fix}])
        run = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_gate_explain", str(path)],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual((run.returncode, run.stderr), (1, ""))
        self.assertEqual(run.stdout.splitlines(), explain(receipt))

    def test_c4_unreadable_or_invalid_receipts_exit_2_without_echoing_the_path(self):
        """C4: missing, linked, oversize, duplicate-field, malformed or inconsistent input is exit 2."""
        receipts = self.produced()
        real = self.write("real.json", receipts["go"])
        link = self.root / "link.json"
        link.symlink_to(real)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "fifo.json"
        os.mkfifo(fifo)
        text = json.dumps(receipts["go"])
        huge = self.write("huge.json", b" " * (1 << 20) + b"{}")
        lies = self.write("lies.json", dict(receipts["go"], verdict="NO-GO"))
        duplicate = self.write("dup.json", text.replace('"verdict": "GO"', '"verdict": "GO", "verdict": "GO"').encode())
        for name, argv in {
            "missing": [self.root / "absent.json"], "symlink": [link], "symlinked dir": [alias / "real.json"],
            "directory": [self.root], "fifo": [fifo], "oversize": [huge], "duplicate": [duplicate],
            "malformed": [self.write("bad.json", b"{")], "non-utf8": [self.write("bin.json", b"\xff")],
            "nan": [self.write("nan.json", b'{"a": NaN}')], "inconsistent": [lies],
            "array": [self.write("list.json", b"[]")],
        }.items():
            with self.subTest(name=name):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("invalid receipt: "))
                self.assertNotIn(str(self.root), err)
        self.assertEqual(self.run_cli(real)[0], 0)  # the healthy control beside every refusal


if __name__ == "__main__":
    unittest.main()
