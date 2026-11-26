"""Runtime contract for the sandbox-only stage 15 rehearsal."""

import builtins
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import rehearse_engine_switch as driver, run_stages

_PAIR = ("run-records.sh", "LIVE.json")
_OLD = "engine-1234567"
_CANDIDATE = "engine-89abcde"


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _capture(root, folder):
    return tuple((root / folder / name).read_bytes() for name in _PAIR)


def _observed(engine, pair):
    return {"wrapper_sha256": _digest(pair[0]), "live_sha256": _digest(pair[1]),
            "engines": [engine], "consistent": True}


class EngineSwitchTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.sandbox = self.new_sandbox("sandbox")

    def new_sandbox(self, name):
        path = self.parent / name
        path.mkdir(mode=0o700)
        return path

    def expected_report(self, root, *, failure=None, tampered=None):
        if tampered is not None:
            # The render step's hash predates the corruption of the backup.
            path, backup_bytes = tampered
            original = {path: backup_bytes}
        else:
            original = {}
        outputs = {
            "render_pairs": [f"{folder}/{name}" for folder in ("backup", "candidate") for name in _PAIR],
            "preswitch": ["preswitch.json"],
            "switch": ["switch.json"],
            "stub_run": ["stub-run.json"],
            "rollback": [f"current/{name}" for name in _PAIR],
            "verify_rollback": [f"current/{name}" for name in _PAIR],
        }
        report = {"stages": ["15"], "steps": [], "manual_steps": [], "codes": []}
        for step, names in outputs.items():
            if failure and step == failure[0]:
                report["codes"] = failure[1]
                report["steps"].append({"name": step, "code": failure[1][0], "outputs": []})
                break
            report["steps"].append({"name": step, "code": "OK", "outputs": [
                {"path": name, "sha256": _digest(original[name] if name in original else (root / name).read_bytes())}
                for name in names
            ]})
        return report

    def test_c1_switch_run_once_and_restore(self):
        """C1: The real checks see the candidate during one stub run and old bytes after rollback."""
        # Detect one-sided switch, reading the candidate archive instead of the
        # current pair, a duplicate stub run, and a rollback that re-renders bytes.
        trace = []
        inspect = driver.golive_pair.inspect

        def observe(wrapper, live):
            trace.append((wrapper.parent.name, wrapper.read_bytes(), live.read_bytes()))
            return inspect(wrapper, live)

        with mock.patch.object(driver, "_stub_run", wraps=driver._stub_run) as stub, \
                mock.patch.object(driver.golive_pair, "inspect", side_effect=observe):
            result = driver.run(self.sandbox)
        old, candidate = (_capture(self.sandbox, label) for label in ("backup", "candidate"))
        self.assertNotEqual(old[0], candidate[0])
        self.assertNotEqual(old[1], candidate[1])
        self.assertEqual(_capture(self.sandbox, "current"), old)
        self.assertEqual(trace, [("current", *old), ("candidate", *candidate),
                                 ("current", *candidate), ("current", *candidate), ("current", *old)])
        stub.assert_called_once()
        self.assertEqual(json.loads((self.sandbox / "preswitch.json").read_text()), {"status": "PASS", "rows": []})
        for receipt in ("switch.json", "stub-run.json"):
            expected = _observed(_CANDIDATE, candidate)
            if receipt == "stub-run.json":
                # The backend's own one-line receipt of the pair it read.
                expected = {"pair_sha256": _digest(candidate[1]), "wrapper_sha256": _digest(candidate[0])}
            self.assertEqual(json.loads((self.sandbox / receipt).read_text()), expected)
        self.assertEqual(inspect(*(self.sandbox / "current" / member for member in _PAIR)), _observed(_OLD, old))
        self.assertEqual(result, self.expected_report(self.sandbox))
        self.assertEqual(driver.STAGES, ("15",))
        self.assertIsInstance(driver.TITLE, str)

    def test_c1_all_opens_stay_in_sandbox(self):
        """C1: The marker comes first and no checkout data or external temp is opened."""
        # Catch calls to the full renderer/preswitch, whose template and temp
        # defaults are outside the sandbox, including Path.open's io.open route.
        opened = []
        real_temp = tempfile.TemporaryDirectory

        def record(path):
            path = Path(path).resolve()
            opened.append(path)
            self.assertTrue(path.is_relative_to(self.sandbox), str(path))

        def guard(opener):
            def open_checked(path, *args, **kwargs):
                # Popen wraps its own pipe descriptors with io.open; those are not paths.
                if not isinstance(path, int):
                    record(path)
                return opener(path, *args, **kwargs)
            return open_checked

        def temp_checked(*args, **kwargs):
            directory = kwargs.get("dir", args[2] if len(args) > 2 else None)
            self.assertIsNotNone(directory, "implicit system temp")
            record(directory)
            return real_temp(*args, **kwargs)

        launched = []
        real_popen = subprocess.Popen

        def popen_checked(argv, *args, **kwargs):
            # The one allowed child process is the sandbox backend, no shell.
            launched.append((argv, kwargs))
            return real_popen(argv, *args, **kwargs)

        with mock.patch.object(builtins, "open", guard(builtins.open)), \
                mock.patch.object(io, "open", guard(io.open)), \
                mock.patch.object(os, "open", guard(os.open)), \
                mock.patch.object(tempfile, "TemporaryDirectory", side_effect=temp_checked), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("network")), \
                mock.patch.object(subprocess, "Popen", side_effect=popen_checked):
            result = driver.run(self.sandbox)
        self.assertEqual(result, self.expected_report(self.sandbox))
        self.assertEqual(len(launched), 1)
        argv, kwargs = launched[0]
        self.assertEqual(argv, [sys.executable, str(self.sandbox / "candidate" / "source" / "campaign_tool" / "stub_backend.py"),
                                str(self.sandbox / "current" / "LIVE.json"), str(self.sandbox / "stub-run.json")])
        self.assertEqual((kwargs["cwd"], kwargs["env"], kwargs["shell"]), (self.sandbox, {}, False))
        self.assertEqual(opened[0], self.sandbox / run_stages.SANDBOX_MARKER)
        self.assertIn(self.sandbox / "inventory.json", opened)
        self.assertIn(self.sandbox / "candidate" / "source" / "campaign_tool" / "stub_backend.py", opened)

    def test_c1_preswitch_refuses_invalid_plan_and_inventory(self):
        """C1: Failed plan and dependency checks leave the starting pair current."""
        # A skipped preflight would run the stub even with an invalid plan or stale reviewed inventory.
        preswitch = driver._preswitch
        for label in ("plan", "inventory"):
            with self.subTest(label=label):
                root = self.new_sandbox(label)
                tampered = None
                save = driver._save_json

                def stale(path, value):
                    if path.name == "inventory.json":
                        value = [{"module": "campaign_tool.stub_backend", "qualname": "<module>",
                                  "top": "synthetic_missing", "fingerprint": "sha256:0000000000000000"}]
                    return save(path, value)

                def run_check(sandbox, candidate):
                    nonlocal tampered
                    if label == "plan":
                        plan = candidate / "LIVE.json"
                        tampered = ("candidate/LIVE.json", plan.read_bytes())
                        plan.write_text('{}\n', encoding="ascii")
                        return preswitch(sandbox, candidate)
                    with mock.patch.object(driver, "_save_json", side_effect=stale):
                        return preswitch(sandbox, candidate)

                with mock.patch.object(driver, "_preswitch", side_effect=run_check):
                    result = driver.run(root)
                self.assertEqual(result, self.expected_report(root, failure=("preswitch", ["PRESWITCH_FAILED"]),
                                                             tampered=tampered))
                self.assertEqual(_capture(root, "current"), _capture(root, "backup"))
                self.assertFalse((root / "stub-run.json").exists())

    def test_c1_backend_runs_on_the_switched_pair_and_rollback_restores_old(self):
        """C1: The sandbox backend is really invoked on the current pair after the switch."""
        # A stub_run that only hashes the pair (never runs the backend), or runs it
        # on the backup/candidate folder instead of current, leaves no matching receipt.
        result = driver.run(self.sandbox)
        self.assertEqual(result["codes"], [])
        old, candidate = (_capture(self.sandbox, label) for label in ("backup", "candidate"))
        receipt = json.loads((self.sandbox / "stub-run.json").read_text())
        self.assertEqual(receipt, {"pair_sha256": _digest(candidate[1]), "wrapper_sha256": _digest(candidate[0])})
        # After rollback the same backend, pointed at current, now reads the old pair.
        backend = self.sandbox / "candidate" / "source" / "campaign_tool" / "stub_backend.py"
        again = self.sandbox / "again.json"
        subprocess.run([sys.executable, str(backend), str(self.sandbox / "current" / "LIVE.json"), str(again)],
                       cwd=self.sandbox, env={}, check=True)
        self.assertEqual(json.loads(again.read_text()), {"pair_sha256": _digest(old[1]), "wrapper_sha256": _digest(old[0])})
        self.assertNotEqual(receipt, json.loads(again.read_text()))

    def test_c1_backend_exits_3_when_the_pair_is_missing(self):
        """C1: The backend refuses a pair file that is not there instead of reporting a hash."""
        backend = self.sandbox / "stub_backend.py"
        backend.write_text(driver._BACKEND, encoding="ascii")
        missing = subprocess.run([sys.executable, str(backend), str(self.sandbox / "nope" / "LIVE.json"),
                                  str(self.sandbox / "out.json")], cwd=self.sandbox, env={}, check=False)
        self.assertEqual(missing.returncode, 3)
        self.assertFalse((self.sandbox / "out.json").exists())

    def test_c1_stub_run_fails_on_bad_backend_and_still_restores_old_pair(self):
        """C1: A failing or wrong-pair backend fails stub_run, and rollback restores the starting pair."""
        # Catch a stub_run that ignores the exit status or trusts the driver's own hashes
        # rather than the hashes the backend reported, or leaves the candidate current.
        wrong = "0" * 64
        cases = {
            "BACKEND_EXIT_NONZERO": "raise SystemExit(5)\n",
            "BACKEND_RESULT_INVALID": "raise SystemExit(0)\n",
            "BACKEND_PAIR_MISMATCH": ("import sys\nopen(sys.argv[2], 'w').write('{\"pair_sha256\": \"%s\", "
                                      "\"wrapper_sha256\": \"%s\"}\\n')\n" % (wrong, wrong)),
        }
        for code, source in cases.items():
            with self.subTest(code=code):
                root = self.new_sandbox(code)
                with mock.patch.object(driver, "_BACKEND", source):
                    result = driver.run(root)
                self.assertEqual(result, self.expected_report(root, failure=("stub_run", [code])))
                self.assertEqual(_capture(root, "current"), _capture(root, "backup"))

    def test_c1_switch_refuses_a_mixed_pair(self):
        """C1: A one-sided copy does not let the stub run."""
        copy = driver._copy_pair

        def wrapper_only(source, destination):
            if source.name == "candidate":
                (destination / _PAIR[0]).write_bytes((source / _PAIR[0]).read_bytes())
            else:
                return copy(source, destination)

        with mock.patch.object(driver, "_copy_pair", side_effect=wrapper_only), \
                mock.patch.object(driver, "_stub_run", wraps=driver._stub_run) as stub:
            result = driver.run(self.sandbox)
        self.assertEqual(result, self.expected_report(self.sandbox, failure=(
            "switch", ["PAIR_LIVE_DRIFT", "PAIR_ENGINE_MIXED"])))
        stub.assert_not_called()

    def test_c1_step_io_failure_stops_at_failed_step(self):
        """C1: A phase I/O failure is reported rather than leaking the exception or continuing."""
        for step, seam in (("render_pairs", "_render_pairs"), ("preswitch", "_preswitch"),
                           ("switch", "_switch"), ("stub_run", "_stub_run"),
                           ("rollback", "_copy_pair"), ("verify_rollback", "_verify_rollback")):
            with self.subTest(step=step):
                root = self.new_sandbox(step)
                original = getattr(driver, seam)
                calls = 0

                def failed(*args):
                    nonlocal calls
                    calls += 1
                    if seam == "_copy_pair" and calls < 3:
                        return original(*args)
                    raise OSError("not part of the report")

                with mock.patch.object(driver, seam, side_effect=failed):
                    self.assertEqual(driver.run(root), self.expected_report(
                        root, failure=(step, [step.upper() + "_FAILED"])))

    def test_c2_tampered_backup_is_reported_by_pair_checker(self):
        """C2: Tampering with either backup byte stream fails verify_rollback."""
        # Keep the old engine string present: an engine-name-only check would pass.
        stub = driver._stub_run
        for member, code in zip(_PAIR, ("PAIR_WRAPPER_DRIFT", "PAIR_LIVE_DRIFT")):
            with self.subTest(member=member):
                root = self.new_sandbox(member)
                backup = root / "backup" / member
                before = []

                def tamper(*args):
                    output = stub(*args)
                    before.append(backup.read_bytes())
                    backup.write_bytes(before[0] + b"\n")
                    return output

                with mock.patch.object(driver, "_stub_run", side_effect=tamper):
                    result = driver.run(root)
                self.assertEqual(result, self.expected_report(root, failure=("verify_rollback", [code]),
                                                             tampered=(f"backup/{member}", before[0])))
                self.assertEqual((root / "current" / member).read_bytes(), before[0] + b"\n")

    def test_c3_outputs_resolve_to_the_same_bytes_after_rollback(self):
        """C3: Every relative evidence path still contains the bytes it hashed."""
        # In particular a switch output that points at mutable current files
        # would become false evidence as soon as rollback replaces those files.
        result = driver.run(self.sandbox)
        self.assertEqual(result["codes"], [])
        for step in result["steps"]:
            self.assertTrue(step["outputs"], step["name"])
            for output in step["outputs"]:
                with self.subTest(step=step["name"], output=output):
                    relative = Path(output["path"])
                    self.assertFalse(relative.is_absolute())
                    path = (self.sandbox / relative).resolve(strict=True)
                    self.assertTrue(path.is_relative_to(self.sandbox))
                    self.assertEqual(output["sha256"], _digest(path.read_bytes()))

    def test_c1_cli_status_and_json(self):
        """C1: New and empty roots succeed, a failed step exits 1 with JSON."""
        # Catch a CLI that stops admitting a new root or reports a failed run as success.
        for mode in ("new", "empty", "failed"):
            with self.subTest(mode=mode), redirect_stdout(io.StringIO()) as stdout:
                root = self.parent / mode if mode == "new" else self.new_sandbox(mode)
                if mode == "failed":
                    with mock.patch.object(driver, "_render_pairs", side_effect=OSError):
                        self.assertEqual(driver.main(["--sandbox", str(root)]), 1)
                    failure = ("render_pairs", ["RENDER_PAIRS_FAILED"])
                else:
                    self.assertEqual(driver.main(["--sandbox", str(root)]), 0)
                    failure = None
                self.assertEqual(json.loads(stdout.getvalue()), self.expected_report(root, failure=failure))
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_c1_refuses_unsafe_sandboxes(self):
        """C1: Direct and CLI callers cannot run in nonempty, aliased, or nonprivate roots."""
        # An existing unrelated file and a symlink target must not be touched.
        nonempty = self.new_sandbox("nonempty")
        sentinel = nonempty / "untouched"
        sentinel.write_bytes(b"unchanged")
        alias = self.parent / "alias"
        alias.symlink_to(self.sandbox, target_is_directory=True)
        nonprivate = self.new_sandbox("nonprivate")
        nonprivate.chmod(0o755)
        for root in (nonempty, alias, nonprivate):
            with self.subTest(root=root.name):
                with self.assertRaises(ValueError) as direct:
                    driver.run(root)
                self.assertIs(type(direct.exception), ValueError)
                with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as cli:
                    driver.main(["--sandbox", str(root)])
                self.assertEqual((cli.exception.code, stdout.getvalue()), (2, ""))
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertEqual(list(nonempty.iterdir()), [sentinel])
        self.assertEqual(list(self.sandbox.iterdir()), [])
        self.assertEqual(list(nonprivate.iterdir()), [])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as missing:
            driver.main([])
        self.assertEqual(missing.exception.code, 2)
