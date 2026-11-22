"""Offline runtime tests for the attended gate contract."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campaign_tool.records import golive_gate as gate


NOW = datetime(2026, 11, 21, 20, tzinfo=timezone.utc)  # Saturday noon Pacific
PIN = hashlib.sha256(b"synthetic copy\n").hexdigest()
PRESENT = {"type": "file_present", "label": "copy", "input": "copy"}
SHA = {"type": "sha256_pin", "label": "pin", "input": "copy", "expected": PIN.upper()}
DISK = {"type": "disk_floor_gib", "label": "space", "input": "space", "min": 1.5}
WINDOW = {"type": "window", "label": "quiet", "days": ["sat"], "start": "12:00", "end": "16:00"}


def spec(*checks):
    return {
        "schema": "records-golive-gate-spec-v1", "gate_id": "fixture-gate",
        "stages": ["3", "A1"], "title": "Synthetic gate", "checks": list(checks),
    }


def result(check, state, code):
    return {"type": check["type"], "label": check["label"], "state": state, "code": code}


def receipt(verdict, results, hashes=None, instant="2026-11-21T20:00:00+00:00"):
    return {
        "schema": "records-golive-gate-v1", "gate_id": "fixture-gate", "stages": ["3", "A1"],
        "evaluated_at": instant, "verdict": verdict, "checks": results, "inputs": hashes or {},
    }


class GateSpecTests(unittest.TestCase):
    def assertBadSpec(self, value, field):
        with self.assertRaisesRegex(ValueError, field) as raised:
            gate.load_spec(value)
        self.assertIs(type(raised.exception), ValueError)

    def test_c1_accepted_spec(self):
        """C1: accept a valid mixed spec without dropping fields."""
        self.assertEqual(gate.load_spec(spec(SHA, DISK, WINDOW, PRESENT)), spec(SHA, DISK, WINDOW, PRESENT))

    def test_c1_top_level_refusals(self):
        """C1: a typo or unbound stage ID cannot silently select a different gate."""
        good = spec(PRESENT)
        cases = (
            (None, "spec must be an object"),
            ({**good, "extra_key": 1}, "unknown fields: 'extra_key'"),
            ({k: v for k, v in good.items() if k != "schema"}, "missing fields: schema"),
            ({**good, "schema": "records-golive-gate-spec-v2"}, "schema"),
            *(({**good, "gate_id": v}, "gate_id") for v in (42, "../escape", "Upper", "a--b")),
            *(({**good, "stages": v}, "stage") for v in ([], "3", [3], ["unlisted-stage"])),
            ({**good, "title": " "}, "title"), ({**good, "checks": {}}, "checks"),
            ({**good, "checks": []}, "checks"),  # no vacuous GO
        )
        for value, field in cases:
            with self.subTest(value=value):
                self.assertBadSpec(value, field)

    def test_c1_check_refusals(self):
        """C1: named errors expose typos rather than ignoring a misconfigured check."""
        cases = (
            ([], "object with a type"), ({"label": "copy"}, "object with a type"),
            ({**PRESENT, "type": []}, r"unknown check type \[\]"),
            ({**PRESENT, "type": "files_present"}, "unknown check type 'files_present'"),
            ({**PRESENT, "inputs": "copy"}, "unknown fields: 'inputs'"),
            ({"type": "file_present", "label": "copy"}, "missing fields: input"),
            ({**PRESENT, "label": " "}, "label"), ({**PRESENT, "input": "a=b"}, "input"),
            ({**PRESENT, "input": None}, "input"),
            *(({**SHA, "expected": v}, "expected") for v in (64, PIN[:-1], "z" * 64)),
            *(({**DISK, "min": v}, "min") for v in (True, "1", -1, float("inf"), float("nan"))),
            *(({**WINDOW, "days": v}, "days") for v in ("sat", [], ["SAT"], ["sat", "sat"], [[]])),
            ({**WINDOW, "start": "9:30"}, "start"), ({**WINDOW, "end": "24:00"}, "end"),
            ({**WINDOW, "end": "12:60"}, "end"), ({**WINDOW, "end": None}, "end"),
            ({**WINDOW, "end": "12:00"}, "start and end must differ"),
        )
        for check, field in cases:
            with self.subTest(check=check):
                self.assertBadSpec(spec(check), field)


class GateFixture(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=Path.cwd().resolve())
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def fixture(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        return path


class GateEvaluationTests(GateFixture):
    def test_c2_all_checks_and_single_snapshot(self):
        """C2: all passes yield GO; reopening a copy could mix two snapshots."""
        copy = self.fixture("sample", b"synthetic copy\n")
        df = b"  Avail\n 1610612736\n"
        space = self.fixture("df.txt", df)
        before = [(p.name, p.stat().st_mtime_ns) for p in sorted(self.root.iterdir())]
        with patch.object(gate, "_read_input", wraps=gate._read_input) as reader:
            got = gate.evaluate(spec(SHA, DISK, WINDOW, PRESENT),
                                {"copy": copy, "space": space, "unused": self.root / "decoy"}, NOW)
        self.assertEqual([c.args for c in reader.call_args_list], [(copy,), (space,)])
        self.assertEqual(got, receipt("GO", [
            result(SHA, "pass", "GATE_SHA256_MATCH"), result(DISK, "pass", "GATE_DISK_SUFFICIENT"),
            result(WINDOW, "pass", "GATE_WINDOW_OPEN"), result(PRESENT, "pass", "GATE_FILE_PRESENT"),
        ], {"copy": PIN, "space": hashlib.sha256(df).hexdigest()}))
        self.assertEqual([(p.name, p.stat().st_mtime_ns) for p in sorted(self.root.iterdir())], before)
        self.assertEqual((copy.read_bytes(), space.read_bytes()), (b"synthetic copy\n", df))

    def test_c2_mismatch_and_unknown_never_go(self):
        """C2: one mismatch or unknown defeats a pass; checks never short-circuit."""
        path = self.fixture("sample", b"changed")
        hashes = {"copy": hashlib.sha256(b"changed").hexdigest()}
        for check, state, code in ((SHA, "fail", "GATE_SHA256_MISMATCH"),
                                   (DISK, "unknown", "GATE_INPUT_MISSING")):
            with self.subTest(check=check):
                self.assertEqual(gate.evaluate(spec(check, PRESENT), {"copy": path}, NOW), receipt("NO-GO", [
                    result(check, state, code), result(PRESENT, "pass", "GATE_FILE_PRESENT"),
                ], hashes))

    def test_c2_refuses_unavailable_or_unsafe_inputs(self):
        """C2: missing, unreadable, symlinked, oversized and special inputs stay unknown."""
        copy = self.fixture("sample", b"synthetic copy\n")
        leaf_link = self.root / "link"
        leaf_link.symlink_to(copy)
        ancestor_link = self.root / "alias"
        ancestor_link.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        oversized = self.root / "oversized"
        with oversized.open("wb") as stream:
            stream.truncate((1 << 20) + 1)
        checks = (SHA, {**DISK, "input": "copy"}, PRESENT)
        expected = receipt("NO-GO", [result(c, "unknown", "GATE_INPUT_MISSING") for c in checks])
        for path in (None, self.root / "absent", leaf_link, ancestor_link / copy.name,
                     self.root, Path("/"), fifo, oversized, self.root / "bad\0name"):
            with self.subTest(path=path):
                self.assertEqual(gate.evaluate(spec(*checks), {} if path is None else {"copy": path}, NOW), expected)
        # EACCES at the filesystem boundary; chmod is unreliable under root.
        real_open = os.open

        def deny_sample(path, *args, **kwargs):
            if path == copy.name:
                raise PermissionError("fixture unreadable")
            return real_open(path, *args, **kwargs)

        with patch.object(gate.os, "open", side_effect=deny_sample):
            self.assertEqual(gate.evaluate(spec(*checks), {"copy": copy}, NOW), expected)

    def test_c2_size_limit_includes_exact_boundary(self):
        """C2: exactly 1 MiB is valid; an exclusive cap would reject the sample."""
        raw = b"x" * (1 << 20)
        path = self.fixture("boundary", raw)
        self.assertEqual(gate.evaluate(spec(PRESENT), {"copy": path}, NOW), receipt("GO", [
            result(PRESENT, "pass", "GATE_FILE_PRESENT"),
        ], {"copy": hashlib.sha256(raw).hexdigest()}))

    def test_c2_metadata_cannot_certify_replaced_or_growing_input(self):
        """C2: a stale pre-open stat cannot certify the descriptor or read length."""
        path = self.root / "growing"
        with path.open("wb") as stream:
            stream.truncate((1 << 20) + 1)
        link = self.root / "swapped"
        link.symlink_to(self.fixture("symlink-target", b"would otherwise pass"))
        stale = SimpleNamespace(st_mode=stat.S_IFREG, st_size=1 << 20)
        expected = receipt("NO-GO", [result(PRESENT, "unknown", "GATE_INPUT_MISSING")])
        with patch.object(gate.os, "stat", return_value=stale):
            with self.subTest(change="symlink before open"):
                self.assertEqual(gate.evaluate(spec(PRESENT), {"copy": link}, NOW), expected)
            with self.subTest(change="growth before fstat"):
                self.assertEqual(gate.evaluate(spec(PRESENT), {"copy": path}, NOW), expected)
            with self.subTest(change="growth after fstat"), patch.object(gate.os, "fstat", return_value=stale):
                self.assertEqual(gate.evaluate(spec(PRESENT), {"copy": path}, NOW), expected)

    def test_c2_disk_byte_floor_and_strict_transcript(self):
        """C2: require one Avail value in bytes; neither fallback digits nor rounded GiB pass."""
        cases = (
            (b" Avail\r\n 1610612736 \r\n", 1.5, "pass", "GATE_DISK_SUFFICIENT"),
            (b"Avail\n1610612735", 1.5, "fail", "GATE_DISK_BELOW_FLOOR"),
            (b"Avail\n107374183", 0.1, "pass", "GATE_DISK_SUFFICIENT"),
            (b"Avail\n107374182", 0.1, "fail", "GATE_DISK_BELOW_FLOOR"),
            (b"Avail\n0\n", 0, "pass", "GATE_DISK_SUFFICIENT"),
            (b"Avail\n-1\n", 0, "fail", "GATE_DISK_BELOW_FLOOR"),
            *((raw, 0, "unknown", "GATE_DISK_UNPARSED") for raw in (
                b"", b"9999999999\n", b"Available\n9999999999\n", b"Avail\n1G\n",
                b"Avail\n1.5\n", b"Avail\n1,000\n", b"Avail\n1 000\n", b"Avail\n-\n",
                b"Avail\n1000\n2000\n", b"Avail\n1000\nwarning\n", b"Avail\n\xff\n",
            )),
        )
        for raw, floor, state, code in cases:
            with self.subTest(raw=raw, floor=floor):
                check = {**DISK, "min": floor}
                path = self.fixture("df.txt", raw)
                self.assertEqual(gate.evaluate(spec(check), {"space": path}, NOW), receipt(
                    "GO" if state == "pass" else "NO-GO", [result(check, state, code)],
                    {"space": hashlib.sha256(raw).hexdigest()},
                ))

    def assertWindows(self, cases):
        for check, instant, opened in cases:
            with self.subTest(check=check, instant=instant):
                now = datetime.fromisoformat(instant + "+00:00").astimezone(timezone(timedelta(hours=9)))
                verdict, state, code = ("GO", "pass", "GATE_WINDOW_OPEN") if opened else (
                    "NO-GO", "fail", "GATE_WINDOW_CLOSED")
                self.assertEqual(gate.evaluate(spec(check), {}, now), receipt(
                    verdict, [result(check, state, code)], instant=instant + "+00:00",
                ))

    def test_c2_window_boundaries_and_overnight_opening_day(self):
        """C2: midnight cannot move an overnight gate to the wrong weekday."""
        overnight = {**WINDOW, "days": ["sun"], "start": "22:00", "end": "02:00"}
        self.assertWindows((
            (WINDOW, "2026-11-21T19:59:59", False), (WINDOW, "2026-11-21T20:00:00", True),
            (WINDOW, "2026-11-21T23:59:59", True), (WINDOW, "2026-11-22T00:00:00", False),
            (WINDOW, "2026-11-22T20:00:00", False),
            (overnight, "2026-11-23T05:59:59", False), (overnight, "2026-11-23T06:00:00", True),
            (overnight, "2026-11-23T08:00:00", True), (overnight, "2026-11-23T09:59:59", True),
            (overnight, "2026-11-23T10:00:00", False), (overnight, "2026-11-22T08:30:00", False),
        ))

    def test_c2_windows_across_dst(self):
        """C2: a fixed UTC offset mishandles the spring gap and fall folds."""
        sunday = {**WINDOW, "days": ["sun"]}
        gap = {**sunday, "start": "02:00", "end": "03:00"}
        morning = {**sunday, "start": "03:00", "end": "04:00"}
        fold = {**sunday, "start": "01:15", "end": "01:45"}
        overnight = {**WINDOW, "start": "23:00", "end": "01:30"}
        self.assertWindows((
            (gap, "2026-03-08T09:59:59", False), (gap, "2026-03-08T10:00:00", False),
            (morning, "2026-03-08T10:00:00", True),
            (fold, "2026-11-01T08:15:00", True), (fold, "2026-11-01T08:45:00", False),
            (fold, "2026-11-01T09:15:00", True), (fold, "2026-11-01T09:45:00", False),
            (overnight, "2026-11-01T08:29:59", True), (overnight, "2026-11-01T09:29:59", True),
            (overnight, "2026-11-01T09:30:00", False),
        ))

    def test_c2_invalid_clock_or_spec_refused(self):
        """C2: evaluation cannot infer host timezone or bypass schema validation."""
        for candidate, now in ((spec(WINDOW), NOW.replace(tzinfo=None)), (spec(WINDOW), None), (spec(), NOW)):
            with self.subTest(now=now):
                with self.assertRaises(ValueError) as raised:
                    gate.evaluate(candidate, {}, now)
                self.assertIs(type(raised.exception), ValueError)


class GateCliTests(GateFixture):
    def command(self, *options):
        return [sys.executable, "-B", "-m", "campaign_tool.records.golive_gate", *options]

    def test_c3_exclusive_private_receipt(self):
        """C3: --out creates a private receipt; a repeat cannot truncate prior evidence."""
        evidence = self.fixture("copy=data", b"")
        source = self.fixture("spec.json", json.dumps(spec(PRESENT)).encode())
        command = self.command("--spec", str(source), "--input", f"copy={evidence}", "--out", str(self.root), "--json")
        run = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        path = self.root / "fixture-gate.json"
        original = path.read_bytes()
        self.assertEqual(original, run.stdout.encode("utf-8"))
        parsed = json.loads(original)
        timestamp = datetime.fromisoformat(parsed["evaluated_at"])
        self.assertEqual(timestamp.utcoffset(), timedelta(0))
        self.assertEqual(parsed, receipt("GO", [result(PRESENT, "pass", "GATE_FILE_PRESENT")],
                                         {"copy": hashlib.sha256(b"").hexdigest()}, timestamp.isoformat()))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        repeated = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual((repeated.returncode, repeated.stdout), (2, ""))
        self.assertEqual(path.read_bytes(), original)

    def test_c3_text_and_no_go_exit(self):
        """C3: unknown exits 1 with every check printed in order, and creates no file."""
        source = self.fixture("spec.json", json.dumps(spec(PRESENT, SHA)).encode())
        run = subprocess.run(self.command("--spec", str(source)), capture_output=True, text=True, timeout=10)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (
            1, 'NO-GO\nfile_present "copy": unknown GATE_INPUT_MISSING\n'
            'sha256_pin "pin": unknown GATE_INPUT_MISSING\n', "",
        ))
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_c3_no_go_receipt_can_be_saved(self):
        """C3: writing NO-GO evidence must not turn the command into success."""
        source = self.fixture("spec.json", json.dumps(spec(PRESENT)).encode())
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(gate.main(["--spec", str(source), "--out", str(self.root), "--json"]), 1)
        path = self.root / "fixture-gate.json"
        self.assertEqual(path.read_text(encoding="utf-8"), stdout.getvalue())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_c3_bad_arguments_spec_and_output(self):
        """C3: invalid invocations exit 2, not an evidence verdict or a clobber."""
        source = self.fixture("spec.json", json.dumps(spec(PRESENT)).encode())
        malformed = self.fixture("malformed", b"{")
        duplicate = self.fixture("duplicate", b'{"schema": 1, "schema": 2}')
        invalid = self.fixture("invalid", b"{}")
        non_utf8 = self.fixture("non-utf8", b"\xff")
        out_link = self.root / "alias"
        out_link.symlink_to(self.root, target_is_directory=True)
        path = self.root / "fixture-gate.json"
        target = self.root / "not-created"
        path.symlink_to(target)
        args = ["--spec", str(source)]
        for argv in ([], ["--spec", str(malformed)], ["--spec", str(duplicate)], ["--spec", str(invalid)],
                     ["--spec", str(non_utf8)], ["--spec", str(target)], args + ["--bad"],
                     args + ["--input", "copy"], args + ["--input", "=p"], args + ["--input", "copy="],
                     args + ["--input", "copy=a", "--input", "copy=b"], args + ["--out", str(target)],
                     args + ["--out", str(out_link)], args + ["--out", str(self.root)]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    gate.main(argv)
                self.assertIs(type(raised.exception), SystemExit)
                self.assertEqual((raised.exception.code, stdout.getvalue()), (2, ""))
        self.assertEqual(os.readlink(path), str(target))
        self.assertFalse(target.exists())


class GateRegistryTests(GateFixture):
    def test_c4_new_type_validates_and_dispatches(self):
        """C4: a hardcoded evaluator may validate an extension but never invoke it."""
        def validate(check):
            if check["prefix"] != "synthetic":
                raise ValueError("bad prefix")

        def handle(check, get_input, now):
            item = get_input(check["input"])
            if item and item.raw.startswith(check["prefix"].encode()) and now == NOW:
                return "pass", "FIXTURE_PREFIX_FOUND"
            return "unknown", "FIXTURE_PREFIX_UNCONFIRMED"

        raw = b"synthetic data"
        path = self.fixture("sample", raw)
        check = {**PRESENT, "type": "test_prefix", "prefix": "synthetic"}
        kind = gate.CheckType(frozenset({"input", "prefix"}), handle, validate)
        with patch.dict(gate.CHECK_TYPES, {"test_prefix": kind}):
            self.assertEqual(gate.evaluate(spec(check), {"copy": path}, NOW), receipt(
                "GO", [result(check, "pass", "FIXTURE_PREFIX_FOUND")], {"copy": hashlib.sha256(raw).hexdigest()},
            ))
            self.assertEqual(gate.evaluate(spec(check), {}, NOW), receipt(
                "NO-GO", [result(check, "unknown", "FIXTURE_PREFIX_UNCONFIRMED")],
            ))
            with self.assertRaisesRegex(ValueError, "bad prefix") as raised:
                gate.evaluate(spec({**check, "prefix": "other"}), {"copy": path}, NOW)
            self.assertIs(type(raised.exception), ValueError)
