"""Gate pack writer: registry, describe, CLI and the private no-clobber spec write."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from campaign_tool.records import golive_gate, golive_gate_pack as pack
from campaign_tool.records import golive_gate_specs_lanes, golive_gate_specs_s5, golive_gate_specs_s6

HEX = "a" * 64
IDS = ("a4-hostname", "c5-portal-apply", "c6-findings-on", "l17-packets", "s10-console",
       "s5-engine-switch", "s5b-backup", "s6a-trust-mount", "s6b-decisions", "s8-units")
DISK_INPUTS = ("disk", "space")
ROOT = Path(pack.__file__).resolve().parents[2]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = pack.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def digest(name):
    return hashlib.sha256(content(name)).hexdigest()


def content(name):
    return b"Avail\n99999999999999\n" if name in DISK_INPUTS else f"synthetic {name}\n".encode()


def pins_for(gate_id):
    """Smallest valid --pin list, hashed to the synthetic input files below."""
    pins = []
    for name in pack.describe(gate_id)["required_pins"]:
        pins.append("disk_floor_gib=1" if name == "disk_floor_gib"
                    else f"{name}={digest(name.removesuffix('_sha256'))}")
    return pins


class PackTestCase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(dir=Path.cwd().resolve())
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root.chmod(0o700)
        self.out = self.root / "out"
        self.out.mkdir(mode=0o700)

    def assert_refused(self, *argv, written=()):
        code, out, err = run(*argv)
        self.assertEqual(code, 2, argv)
        self.assertEqual((out, err.count("\n"), "Traceback" in err), ("", 1, False), (argv, err))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(written), argv)


class RegistryAndDescribeTests(PackTestCase):
    def test_c1_registry_maps_all_ten_gates_and_refuses_duplicates(self):
        """C1: a gate defined by two packs must not silently pick one pack's spec()."""
        found = pack.registry()
        self.assertEqual(sorted(found), list(IDS))
        self.assertIs(found["s5b-backup"], golive_gate_specs_s5)
        self.assertIs(found["s6b-decisions"], golive_gate_specs_s6)
        self.assertIs(found["c5-portal-apply"], golive_gate_specs_lanes)
        with mock.patch.object(pack, "PACKS", (golive_gate_specs_s5, golive_gate_specs_s5)):
            with self.assertRaisesRegex(ValueError, "more than one pack"):
                pack.registry()
        self.assertEqual(run("--list")[0], 0)  # control: the real packs have no collision

    def test_c2_describe_pins_and_inputs_per_pack_convention(self):
        """C2: operators learn the pin names and inputs from describe, not from reading spec()."""
        self.assertEqual(pack.describe("s5-engine-switch"), {
            "gate_id": "s5-engine-switch", "stages": ["15"], "title": "Sitting 5 engine switch",
            "required_pins": ["live", "wrapper"], "optional_pins": [],
            "inputs": ["disk", "evidence", "live", "pair", "wrapper"]})
        self.assertEqual(pack.describe("s6a-trust-mount")["required_pins"],
                         ["disk_floor_gib", "live_json_sha256", "run_records_sha256"])
        self.assertEqual(pack.describe("s6a-trust-mount")["inputs"],
                         ["evidence", "live_json", "pair", "run_records", "space"])
        self.assertEqual(pack.describe("s6b-decisions")["optional_pins"], ["max_age_days"])
        self.assertEqual(pack.describe("s8-units")["optional_pins"], [])
        portal = pack.describe("c5-portal-apply")
        self.assertEqual((portal["required_pins"], portal["optional_pins"], portal["inputs"]),
                         ([], ["max_age_days"], ["evidence", "hold_release", "pair"]))
        self.assertEqual(pack.describe("s5b-backup")["inputs"], ["disk", "evidence", "pair", "quiescence"])
        with self.assertRaises(KeyError):
            pack.describe("nope")

    def test_c2_described_required_pins_are_exactly_what_spec_needs(self):
        """C2: describe drifting from spec() would send operators to a refusal or a wrong spec."""
        for gate_id in IDS:
            with self.subTest(gate_id=gate_id):
                built = pack.build(gate_id, pins_for(gate_id))
                self.assertEqual(golive_gate.load_spec(built)["gate_id"], gate_id)
                for name in pack.describe(gate_id)["required_pins"]:
                    rest = [pin for pin in pins_for(gate_id) if not pin.startswith(name + "=")]
                    with self.assertRaisesRegex(ValueError, "requires pin"):
                        pack.build(gate_id, rest)


class CliWriteTests(PackTestCase):
    def test_c3_list_prints_one_line_per_gate(self):
        """C3: --list is the operator's index; each line carries ID, stages, title, required pins."""
        code, out, err = run("--list")
        rows = [line.split("\t") for line in out.splitlines()]
        self.assertEqual((code, err, [row[0] for row in rows]), (0, "", list(IDS)))
        self.assertEqual(rows[5], ["s5-engine-switch", "15", "Sitting 5 engine switch", "live,wrapper"])
        self.assertEqual(rows[8][:2], ["s6b-decisions", "A5,A6"])
        self.assertTrue(all(len(row) == 4 for row in rows))

    def test_c3_without_out_prints_spec_json_and_writes_nothing(self):
        """C3: stdout mode must be the same validated spec as the file mode."""
        code, out, err = run("--gate", "s6b-decisions", "--pin", "max_age_days=3")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), pack.build("s6b-decisions", ["max_age_days=3"]))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_c3_c5_every_gate_written_once_round_trips_and_evaluates(self):
        """C3/C5: each registered gate writes a private file that load_spec and golive_gate accept."""
        inputs = self.root / "inputs"
        inputs.mkdir(mode=0o700)
        (inputs / "evidence").mkdir()
        for gate_id in IDS:
            with self.subTest(gate_id=gate_id):
                facts = pack.describe(gate_id)
                code, out, err = run("--gate", gate_id, *[a for p in pins_for(gate_id) for a in ("--pin", p)],
                                     "--out", str(self.out))
                target = self.out / f"{gate_id}.spec.json"
                self.assertEqual((code, out, err), (0, f"{target}\n", ""))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                written = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(golive_gate.load_spec(written), pack.build(gate_id, pins_for(gate_id)))
                argv = ["--spec", str(target), "--json"]
                for name in facts["inputs"]:
                    if name != "evidence":
                        (inputs / name).write_bytes(content(name))
                    argv += ["--input", f"{name}={inputs / name}"]
                receipt, captured = None, io.StringIO()
                with redirect_stdout(captured):
                    status = golive_gate.main(argv)
                receipt = json.loads(captured.getvalue())
                self.assertIn(status, (0, 1))
                self.assertEqual(receipt["gate_id"], gate_id)
                self.assertEqual([c["type"] for c in receipt["checks"]], [c["type"] for c in written["checks"]])
                for check in receipt["checks"]:
                    if check["type"] in ("sha256_pin", "disk_floor_gib", "file_present"):
                        self.assertEqual(check["state"], "pass", check)
        self.assertEqual(len(list(self.out.glob("*.spec.json"))), 10)

    def test_c3_write_is_mode_0600_even_under_a_restrictive_umask(self):
        """C3: a 0400 file would make a retry impossible; the writer sets 0600 explicitly."""
        old = os.umask(0o277)
        self.addCleanup(os.umask, old)
        self.assertEqual(run("--gate", "s8-units", "--out", str(self.out))[0], 0)
        self.assertEqual(stat.S_IMODE((self.out / "s8-units.spec.json").stat().st_mode), 0o600)

    def test_c3_module_entry_point_runs_as_dash_m(self):
        """C3: the documented `python3 -B -m` form works and refusal is a clean exit 2."""
        base = [sys.executable, "-B", "-m", "campaign_tool.records.golive_gate_pack"]
        ok = subprocess.run([*base, "--gate", "s8-units"], cwd=ROOT, capture_output=True, text=True)
        bad = subprocess.run([*base, "--gate", "x"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stderr, json.loads(ok.stdout)["gate_id"]), (0, "", "s8-units"))
        self.assertEqual((bad.returncode, bad.stdout, bad.stderr.count("\n"), "Traceback" in bad.stderr),
                         (2, "", 1, False))

    def test_c4_numeric_pins_parse_as_numbers_and_others_stay_strings(self):
        """C4: int stays int (and 0 is allowed); a float stays float; a string never turns numeric."""
        pins = [f"run_records_sha256={HEX}", f"live_json_sha256={HEX.upper()}"]
        for text, value in (("2", 2), ("0", 0), ("0.5", 0.5), ("1e1", 10.0), ("007", 7)):
            built = pack.build("s6a-trust-mount", [*pins, f"disk_floor_gib={text}", f"max_age_days={text}"])
            floor = next(c["min"] for c in built["checks"] if c["type"] == "disk_floor_gib")
            self.assertEqual((floor, type(floor)), (value, type(value)), text)
            self.assertIs(type(built["checks"][0]["max_age_days"]), type(value))
        self.assertEqual(built["checks"][2]["expected"], HEX.upper())

    def test_c4_refusals_leave_no_file_and_no_traceback(self):
        """C4: every refusal is exit 2, one stderr line, no stdout, and no file created."""
        sha = f"run_records_sha256={HEX}", f"live_json_sha256={HEX}"
        part_a = ["--gate", "s6a-trust-mount", "--pin", sha[0], "--pin", sha[1]]
        cases = {
            "duplicate pin": ["--gate", "s6b-decisions", "--pin", "max_age_days=1", "--pin", "max_age_days=2"],
            "duplicate with same value": ["--gate", "s6b-decisions", "--pin", "max_age_days=1", "--pin", "max_age_days=1"],
            "missing required (all)": ["--gate", "s5-engine-switch"],
            "missing required (one)": ["--gate", "s5-engine-switch", "--pin", f"wrapper={HEX}"],
            "missing disk floor": part_a,
            "unknown pin": ["--gate", "c5-portal-apply", "--pin", "hold_release=x"],
            "age pin on pack that takes none": ["--gate", "s8-units", "--pin", "max_age_days=1"],
            "unknown gate": ["--gate", "s99-nope"],
            "no mode": [],
            "both modes": ["--list", "--gate", "s8-units"],
            "list with pin": ["--list", "--pin", "a=b"],
            "pin without gate": ["--pin", "a=b"],
            "pin without equals": ["--gate", "s8-units", "--pin", "max_age_days"],
            "pin without name": ["--gate", "s8-units", "--pin", "=1"],
            "newline in pin name": ["--gate", "s8-units", "--pin", "a\nb=1"],
            "unknown option": ["--gate", "s8-units", "--bogus"],
            "empty out": ["--gate", "s8-units", "--out", ""],
            "short sha": ["--gate", "s5-engine-switch", "--pin", f"wrapper={HEX}", "--pin", "live=abc"],
            "non-hex sha": ["--gate", "s5-engine-switch", "--pin", f"wrapper={HEX}", "--pin", "live=" + "g" * 64],
            "empty sha": ["--gate", "s5-engine-switch", "--pin", f"wrapper={HEX}", "--pin", "live="],
        }
        for pin in ("disk_floor_gib", "max_age_days"):
            floor = ["--pin", "disk_floor_gib=1"] if pin == "max_age_days" else []
            for bad in ("-1", "-0", "nan", "inf", "1e999", "abc", "", " 1", "1 ", "1\n", "1_0", "0x10", "1,5",
                        "\u0661", "9" * 5000, "+1", ".5", "1."):
                cases[f"{pin}={bad[:20]!r}"] = [*part_a, *floor, "--pin", f"{pin}={bad}"]
        for label, argv in cases.items():
            with self.subTest(label):
                self.assert_refused(*argv, *(() if "--out" in argv else ("--out", str(self.out))))

    def test_c4_failed_write_leaves_no_partial_file(self):
        """C4: an I/O error after the file is created must not leave a truncated spec behind."""
        with mock.patch.object(pack.os, "fchmod", side_effect=OSError(5, "Input/output error")):
            self.assert_refused("--gate", "s8-units", "--out", str(self.out))

    def test_c4_refuses_existing_target_without_touching_it(self):
        """C4: no clobber; a second run never replaces an operator's already-saved spec."""
        target = self.out / "s8-units.spec.json"
        target.write_text("operator copy\n")
        self.assert_refused("--gate", "s8-units", "--out", str(self.out), written=["s8-units.spec.json"])
        self.assertEqual(target.read_text(), "operator copy\n")
        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assertEqual(run("--gate", "s8-units", "--out", str(fresh))[0], 0)  # healthy control
        self.assertEqual(run("--gate", "s8-units", "--out", str(fresh))[0], 2)  # second run refuses

    def test_c4_refuses_bad_directories_and_symlinks(self):
        """C4: --out never follows a link, whether it is the target, the directory or an ancestor."""
        victim = self.root / "victim.txt"
        victim.write_text("keep\n")
        (self.out / "s8-units.spec.json").symlink_to(victim)
        (self.out / "s5b-backup.spec.json").symlink_to(self.root / "dangling")
        for gate_id in ("s8-units", "s5b-backup"):
            self.assert_refused("--gate", gate_id, "--out", str(self.out),
                                written=["s8-units.spec.json", "s5b-backup.spec.json"])
        self.assertEqual((victim.read_text(), (self.root / "dangling").exists()), ("keep\n", False))
        (self.root / "link").symlink_to(self.out)
        (self.root / "file").write_text("x")
        for bad in (self.root / "link", self.root / "link" / "child", self.root / "file",
                    self.root / "missing", self.root / "file" / "sub"):
            with self.subTest(bad=bad.name):
                code, out, err = run("--gate", "s10-console", "--out", str(bad))
                self.assertEqual((code, out, err.count("\n"), "Traceback" in err), (2, "", 1, False))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["s5b-backup.spec.json", "s8-units.spec.json"])
        self.assertFalse((self.root / "missing").exists())


class PortalHoldReleaseTests(PackTestCase):
    def test_c5_portal_spec_written_unchanged_with_hold_release_check(self):
        """C5: the written portal file keeps the hold-release check, with and without an age pin."""
        for pins in ((), ("--pin", "max_age_days=2")):
            for name in list(self.out.iterdir()):
                name.rename(self.root / f"moved-{len(pins)}-{name.name}")
            self.assertEqual(run("--gate", "c5-portal-apply", *pins, "--out", str(self.out))[0], 0)
            written = json.loads((self.out / "c5-portal-apply.spec.json").read_text(encoding="utf-8"))
            hold = {"type": "file_present", "label": "Operator hold-release record", "input": "hold_release"}
            self.assertEqual(written["checks"][0], hold)
            self.assertEqual([c["type"] for c in written["checks"]], ["file_present", "window", "prior_gate", "evidence"])
            self.assertEqual(written["checks"][2]["max_age_days"], 2 if pins else 14)

    def test_c5_a_pack_that_drops_a_check_is_refused_before_any_write(self):
        """C5: if a pack's spec() ever lost the hold-release check, no file would be saved."""
        def dropped(gate_id, **pins):
            value = golive_gate_specs_lanes.GATES[gate_id].copy()
            return {**value, "checks": value["checks"][1:]}
        with mock.patch.object(golive_gate_specs_lanes, "spec", dropped):
            self.assert_refused("--gate", "c5-portal-apply", "--out", str(self.out))


if __name__ == "__main__":
    unittest.main()
