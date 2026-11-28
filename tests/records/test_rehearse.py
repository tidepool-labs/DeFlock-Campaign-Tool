"""Runner contracts, with synthetic drivers and the checkout's real drivers."""

import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from campaign_tool.records import rehearse, rehearsal_receipt

ENGINE = "a" * 40
DRIVER = '''\
import hashlib
from campaign_tool.records.run_stages import SANDBOX_MARKER

STAGES = ('3', '16')
TITLE = 'Synthetic rehearsal'

def run(sandbox):
    assert not list(sandbox.iterdir()), 'sandbox was reused'
    assert sandbox.stat().st_mode & 0o777 == 0o700
    (sandbox / SANDBOX_MARKER).touch(mode=0o600)
    proof = b'synthetic evidence'
    (sandbox / 'proof.bin').write_bytes(proof)
    return {'stages': list(STAGES), 'manual_steps': [], 'codes': [],
            'steps': [{'name': 'synthetic', 'code': 'OK', 'outputs': [
                {'path': 'proof.bin', 'sha256': hashlib.sha256(proof).hexdigest()}]}]}
'''


def invoke(*args):
    with redirect_stdout(io.StringIO()) as output:
        status = rehearse.main([str(arg) for arg in args])
    return status, output.getvalue()


class SyntheticDrivers(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.out = self.root / "runs"
        self.pkg = "runner_test_drivers"
        package = ModuleType(self.pkg)
        package.__path__ = [str(self.root)]
        sys.modules[self.pkg] = package
        self.addCleanup(sys.modules.pop, self.pkg)
        self.enterContext(patch.multiple(rehearse, __file__=str(self.root / "rehearse.py"), __package__=self.pkg))
        self.engine = self.enterContext(patch.object(rehearsal_receipt, "engine_identity", return_value=ENGINE))

    def driver(self, name, source=DRIVER):
        path = self.root / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        self.addCleanup(sys.modules.pop, f"{self.pkg}.{name}", None)
        importlib.invalidate_caches()
        return path

    def refusal(self, *args, error):
        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as exc:
            invoke(*args)
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(stderr.getvalue().splitlines()[-1].partition(": error: ")[2], error)
        self.assertFalse(self.out.exists())

    def check_receipt(self, row):
        path = Path(row.pop("receipt"))
        self.assertEqual(path.parent.parent, self.out)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(rehearsal_receipt.read(path), row)
        return path.parent

    def test_discovery_pattern_and_order(self):
        """C1: Similar stems and packages must not enter the sorted registry."""
        for name in ("rehearse_z1_", "rehearse_a", "rehearse", "rehearse_1", "rehearse_Z", "rehearse_bad-tail"):
            self.driver(name)
        package = self.root / "rehearse_folder"
        package.mkdir()
        (package / "__init__.py").write_text("raise RuntimeError()", encoding="utf-8")
        self.assertEqual(tuple(m.__name__ for m in rehearse.discover()),
                         (f"{self.pkg}.rehearse_a", f"{self.pkg}.rehearse_z1_"))

    def test_malformed_exports_refused(self):
        """C1: An export that is missing or has the wrong type cannot be selected."""
        for index, override in enumerate(("del STAGES", "STAGES = ['3']", "STAGES = ()", "STAGES = (3,)",
                                          "del TITLE", "TITLE = 7", "del run", "run = 'not callable'")):
            name = f"rehearse_bad_{index}"
            # Each prior fixture becomes valid before adding the next malformed one.
            with self.subTest(override=override):
                path = self.driver(name, DRIVER + override + "\n")
                with self.assertRaisesRegex(ValueError, rf"^Invalid rehearsal driver {name}:"):
                    rehearse.discover()
                path.write_text(DRIVER, encoding="utf-8")
                sys.modules.pop(f"{self.pkg}.{name}", None)

    def test_import_error_refused(self):
        """C1: Broken matching modules must not silently disappear from discovery."""
        self.driver("rehearse_broken", "raise ImportError('synthetic detail')\n")
        self.refusal("--list", error="Invalid rehearsal driver rehearse_broken: ImportError")

    def test_shadow_package_refused(self):
        """C1: An import resolving to a package instead of its .py driver must not run."""
        self.driver("rehearse_shadow")
        package = self.root / "rehearse_shadow"
        package.mkdir()
        (package / "__init__.py").write_text(DRIVER, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "^Invalid rehearsal driver rehearse_shadow: ImportError$"):
            rehearse.discover()

    def test_symlink_refused_without_import(self):
        """C1: A matching symlink must not execute its target's top-level code."""
        target = self.driver("decoy", "raise AssertionError('symlink was executed')\n")
        (self.root / "rehearse_link.py").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "^Invalid rehearsal driver rehearse_link: symlinked source$"):
            rehearse.discover()

    def test_list_is_read_only_and_filters(self):
        """C2: Listing by stage must not execute drivers or create --out."""
        self.driver("rehearse_a", DRIVER + "\ndef run(sandbox): raise AssertionError()\n")
        self.driver("rehearse_b", DRIVER + "\nSTAGES = ('20',)\n")
        self.assertEqual(invoke("--list", "--stage", "16", "--out", self.out, "--json"), (0, json.dumps({
            "module": "rehearse_a", "stages": ["3", "16"], "title": "Synthetic rehearsal",
        }) + "\n"))
        self.assertEqual(invoke("--list", "--stage", "20"), (0, "rehearse_b 20: Synthetic rehearsal\n"))
        self.assertFalse(self.out.exists())
        self.engine.assert_not_called()

    def test_unmatched_stage_refuses_before_running_anything(self):
        """C2: A typo in a repeated --stage must not report a partial green."""
        self.driver("rehearse_a")
        self.refusal("--out", self.out, "--stage", "3", "--stage", "999", error="no rehearsal driver for stage(s): 999")

    def test_missing_output_or_drivers_refused(self):
        """C2: Neither a missing output root nor an empty run reports success."""
        self.refusal("--out", self.out, error="no rehearsal drivers found")
        self.assertEqual(invoke("--list"), (0, ""))
        self.driver("rehearse_a")
        self.refusal(error="--out is required unless --list is used")

    def unusable_out(self, out):
        """Refuse an unusable --out with one parser line, before any driver or engine lookup runs."""
        before = sorted(self.root.rglob("*"))
        with patch.object(rehearse, "_run_driver") as run_driver, \
                redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as exc:
            invoke("--out", out, "--stage", "3")
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(stderr.getvalue().splitlines()[-1].partition(": error: ")[2], "--out is not a usable directory")
        run_driver.assert_not_called()
        self.engine.assert_not_called()
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_out_that_is_a_file_refused(self):
        """Unit C1: --out naming a regular file exits 2 with one line, not a FileExistsError traceback."""
        self.driver("rehearse_a")
        self.out.write_text("not a directory", encoding="utf-8")
        self.unusable_out(self.out)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "not a directory")

    def test_out_under_a_file_parent_refused(self):
        """Unit C1: a parent chain that cannot be created (regular-file parent) gets the same refusal."""
        self.driver("rehearse_a")
        (self.root / "parent").write_text("not a directory", encoding="utf-8")
        self.unusable_out(self.root / "parent" / "child" / "runs")

    def test_unwritable_out_stops_after_kept_receipts(self):
        """Unit C2: a sandbox failure prints one stderr line, returns 2, keeps earlier receipts and stops."""
        self.driver("rehearse_a")
        self.driver("rehearse_b", DRIVER + "\nSTAGES = ('20',)\n")
        self.driver("rehearse_c", DRIVER + "\nSTAGES = ('21',)\n")
        mkdtemp, calls = tempfile.mkdtemp, []

        def second_fails(*args, **kwargs):
            # tempfile is shared with the receipt writer; only count the runner's sandbox calls.
            if kwargs.get("prefix", "").startswith("rehearse_"):
                calls.append(kwargs["prefix"])
                if len(calls) == 2:
                    raise PermissionError(13, "synthetic")
            return mkdtemp(*args, **kwargs)

        with patch.object(rehearse.tempfile, "mkdtemp", side_effect=second_fails), \
                redirect_stderr(io.StringIO()) as stderr:
            status, output = invoke("--out", self.out, "--json")
        self.assertEqual((status, stderr.getvalue()), (2, "rehearse: cannot create a sandbox under --out\n"))
        self.assertEqual(calls, ["rehearse_a-", "rehearse_b-"])  # rehearse_c never started
        self.assertEqual(self.check_receipt(json.loads(output)).parent, self.out)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertEqual(list(self.out.iterdir()), [self.check_receipt(json.loads(output))])

    def test_directory_without_write_permission_returns_2(self):
        """Unit C2: a real mode-0500 --out reports the sandbox line, not a traceback, and writes nothing."""
        if os.geteuid() == 0:
            self.skipTest("running as root: mode 0500 does not stop root from creating sandboxes")
        self.driver("rehearse_a")
        self.out.mkdir(mode=0o700)
        self.out.chmod(0o500)
        self.addCleanup(self.out.chmod, 0o700)  # run before the temporary root is removed
        if os.access(self.out, os.W_OK):
            self.skipTest("filesystem ignores mode 0500 for this user (ACL or capability)")
        with redirect_stderr(io.StringIO()) as stderr:
            status, output = invoke("--out", self.out)
        self.assertEqual((status, output, stderr.getvalue()), (2, "", "rehearse: cannot create a sandbox under --out\n"))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_stage_selection_and_fresh_repeated_runs(self):
        """C2: Overlapping stage filters run a driver once; repeats keep distinct evidence."""
        self.driver("rehearse_a")
        self.driver("rehearse_b", DRIVER + "\nSTAGES = ('20',)\n")
        # The writer's filenames use second precision; force a same-second replay.
        clock = self.enterContext(patch.object(rehearse, "datetime"))
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sandboxes = set()
        for _ in range(2):
            status, output = invoke("--out", self.out, "--stage", "16", "--stage", "3", "--json")
            self.assertEqual(status, 0)
            row = json.loads(output)
            self.assertEqual((row["module"], row["stages"], row["engine"], row["result"],
                              row["qualified"], row["qualify_codes"]), ("rehearse_a", ["3", "16"], ENGINE, "pass", True, []))
            self.assertEqual(row["steps"][0]["outputs"][0]["path"], "proof.bin")
            sandbox = self.check_receipt(row)
            self.assertNotIn(sandbox, sandboxes)
            sandboxes.add(sandbox)
        self.assertEqual(set(self.out.iterdir()), sandboxes)

    def test_passing_step_without_engine_is_not_qualified(self):
        """C2: A passing driver must still exit 1 if provenance cannot be established."""
        self.driver("rehearse_a")
        self.engine.return_value = None
        status, text = invoke("--out", self.out)
        summary, separator, path = text.rstrip("\n").rpartition(" ")
        self.assertEqual((status, summary, separator),
                         (1, "rehearse_a 3,16 pass NOT QUALIFIED REHEARSAL_ENGINE_UNKNOWN", " "))
        row = rehearsal_receipt.read(path)
        self.assertEqual((row["engine"], row["qualified"], row["qualify_codes"]),
                         (None, False, ["REHEARSAL_ENGINE_UNKNOWN"]))

    def test_driver_return_is_preserved_on_failure(self):
        """C2: A normal non-OK return is retained verbatim, not relabelled as a crash."""
        self.driver("rehearse_a", DRIVER + '''\
seed = run
def run(sandbox):
    result = seed(sandbox)
    result['steps'] = [{'name': 'verify', 'code': 'VERIFY_FAILED', 'outputs': []}]
    result['manual_steps'] = ['Review synthetic evidence']
    result['codes'] = ['VERIFY_FAILED']
    return result
''')
        status, output = invoke("--out", self.out, "--json")
        row = json.loads(output)
        self.assertEqual((status, row["result"], row["qualified"], row["qualify_codes"], row["steps"],
                          row["manual_steps"], row["codes"]),
                         (1, "fail", False, ["REHEARSAL_FAILED_STEP", "REHEARSAL_MANUAL_STEP"],
                          [{"name": "verify", "code": "VERIFY_FAILED", "outputs": []}],
                          ["Review synthetic evidence"], ["VERIFY_FAILED"]))
        self.check_receipt(row)

    def test_crash_is_redacted_and_a_later_driver_runs_in_isolation(self):
        """C3: A crash after writing proof leaves a failure receipt, not a tainted sibling."""
        self.driver("rehearse_a", DRIVER + '''\
seed = run
def run(sandbox):
    seed(sandbox)
    raise RuntimeError('do not echo this synthetic exception message')
''')
        self.driver("rehearse_b", DRIVER)
        status, output = invoke("--out", self.out, "--json")
        self.assertEqual(status, 1)
        self.assertNotIn("do not echo", output)
        failed, passed = map(json.loads, output.splitlines())
        self.assertEqual((failed["module"], failed["result"], failed["qualified"], failed["qualify_codes"],
                          failed["steps"], failed["manual_steps"], failed["codes"]),
                         ("rehearse_a", "fail", False, ["REHEARSAL_FAILED_STEP"],
                          [{"name": "RuntimeError", "code": "REHEARSAL_DRIVER_ERROR", "outputs": []}],
                          [], ["REHEARSAL_DRIVER_ERROR"]))
        self.assertEqual((passed["module"], passed["result"], passed["qualified"], passed["qualify_codes"]),
                         ("rehearse_b", "pass", True, []))
        self.assertNotEqual(self.check_receipt(failed), self.check_receipt(passed))


class CheckoutDrivers(unittest.TestCase):
    def test_real_drivers_run_and_write_receipts(self):
        """C2: Every checkout driver must compose with the runner, without a hard-coded registry."""
        drivers = rehearse.discover()
        with tempfile.TemporaryDirectory() as tmp, patch.object(rehearsal_receipt, "engine_identity", return_value=ENGINE):
            out = Path(tmp).resolve()
            if not drivers:
                self.assertEqual(invoke("--list", "--json"), (0, ""))
                return
            status, output = invoke("--out", out, "--json")
            rows = [json.loads(line) for line in output.splitlines()]
            self.assertEqual([row["module"] for row in rows], [driver.__name__.rsplit(".", 1)[-1] for driver in drivers])
            self.assertEqual(status, 0 if all(row["qualified"] for row in rows) else 1)
            parents = set()
            for row in rows:
                with self.subTest(driver=row["module"]):
                    path = Path(row.pop("receipt"))
                    self.assertEqual(rehearsal_receipt.read(path), row)
                    self.assertNotIn("REHEARSAL_DRIVER_ERROR", row["codes"])
                    self.assertTrue(row["steps"])
                    parents.add(path.parent)
            self.assertEqual(parents, set(out.iterdir()))
            self.assertEqual(len(parents), len(drivers))
