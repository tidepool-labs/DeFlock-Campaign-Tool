"""Runtime checks for the Stage 3 rehearsal, not for installed systemd units."""

import contextlib
import hashlib
import io
import json
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import arrival, rehearse_arrival as driver, run_report, run_stages

_SINGLE = "run_20000101T000000Z_00000001"
_BURST = "run_20000101T000000Z_00000002"
_STAMP = "20000101T000000.000000Z-mail.json"
_SECOND = "20000101T000000.000000Z-mail-01.json"
_CLOCK = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _files_for_claim(run_id, *names):
    done = "runtime/arrival/done"
    return [f"runtime/{run_id}.arrivals.json", f"{done}/.{run_id}.claim",
            *(f"{done}/{run_id}/{name}" for name in names)]


_OUTPUTS = {
    "render_units": [run_stages.SANDBOX_MARKER, "stub.json", "units/mail-arrival-stamp.sh",
                     "units/records-arrival.path", "units/records-arrival.service",
                     "units/records-health.service", "units/records-health.timer",
                     "units/records-run.service", "units/records-run.timer", "units/run-records.sh",
                     "units/targets/TEST.json"],
    "write_stamp": ["mail-stamp.json", "runtime/arrival.lock"],
    "consume": _files_for_claim(_SINGLE, _STAMP),
    "run_report": [f"runtime/{_SINGLE}.report.json"],
    "batch_two": [*_files_for_claim(_BURST, _STAMP, _SECOND), f"runtime/{_BURST}.report.json"],
    "coordination_quiet": ["runtime/arrival.lock", "runtime/arrival.pending"],
}


class ArrivalRehearsalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rehearse-arrival-")
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name).resolve()
        self.root = self.new_sandbox("sandbox")
        self.runtime = self.root / "runtime"
        self.queue = self.runtime / "arrival"
        self.enterContext(mock.patch.object(arrival, "_utc_now", return_value=_CLOCK))
        self.proc = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError))
        self.socket = self.enterContext(mock.patch.object(socket, "socket", side_effect=AssertionError))

    def new_sandbox(self, name):
        root = self.parent / name
        root.mkdir(mode=0o700)
        return root

    def expected_result(self, root, failed=None):
        steps = []
        codes = []
        for name, paths in _OUTPUTS.items():
            if name == failed:
                codes.append(name.upper() + "_FAILED")
                steps.append({"name": name, "code": codes[0], "outputs": []})
                break
            outputs = [{"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
                       for path in sorted(paths)]
            steps.append({"name": name, "code": "OK", "outputs": outputs})
        return {"stages": ["3"], "steps": steps, "manual_steps": [], "codes": codes}

    def test_c1_render_claim_and_one_report_per_wake(self):
        """C1: Watch the real queue; archive one then two stamps in exactly two runs."""
        # A route to another directory, reversed stamp/claim order, or one run
        # per stamp breaks the observed service graph or the on-disk inventory.
        claim = self.enterContext(mock.patch.object(arrival, "claim", wraps=arrival.claim))
        touch = Path.touch

        def mark_first(path, **kwargs):
            self.assertEqual(list(self.root.iterdir()), [])
            touch(path, **kwargs)

        with mock.patch.object(Path, "touch", autospec=True, side_effect=mark_first) as mark:
            result = driver.run(self.root)
        self.assertEqual(result, self.expected_result(self.root))
        self.assertEqual(driver.STAGES, ("3",))
        self.assertIs(type(driver.TITLE), str)
        mark.assert_called_once_with(self.root / run_stages.SANDBOX_MARKER, mode=0o600, exist_ok=False)
        self.proc.assert_not_called()
        self.socket.assert_not_called()
        self.assertEqual({path.name for path in self.parent.iterdir()}, {"sandbox"})
        units = self.root / "units"
        path_text = (units / "records-arrival.path").read_text()
        self.assertEqual([line for line in path_text.splitlines() if line.startswith("PathChanged=")],
                         [f"PathChanged={self.root / 'drop'}", f"PathChanged={self.queue}"])
        self.assertIn("Unit=records-arrival.service", path_text.splitlines())
        service = (units / "records-arrival.service").read_text()
        self.assertIn(f"ExecStart=/bin/sh {self.runtime / 'run-records.sh'} --arrival", service.splitlines())
        self.assertEqual(claim.call_args_list, [mock.call(self.runtime, _SINGLE), mock.call(self.runtime, _BURST)])
        self.assertEqual(sorted(path.name for path in self.queue.iterdir()), ["done"])
        self.assertEqual(sorted(path.name for path in (self.queue / "done").iterdir()),
                         [f".{_SINGLE}.claim", f".{_BURST}.claim", _SINGLE, _BURST])
        self.assertEqual(sorted(path.name for path in self.runtime.glob("*.report.json")),
                         [f"{_SINGLE}.report.json", f"{_BURST}.report.json"])
        self.assertEqual((self.root / "mail-stamp.json").read_bytes(),
                         (self.queue / "done" / _SINGLE / _STAMP).read_bytes())
        for run_id, filenames, labels in [(_SINGLE, [_STAMP], ["single"]),
                                           (_BURST, [_SECOND, _STAMP], ["burst-two", "burst-one"])]:
            with self.subTest(run_id=run_id):
                self.assertEqual(json.loads((self.queue / "done" / f".{run_id}.claim").read_bytes()), filenames)
                expected = [{"source": "mail", "ref": hashlib.sha256(label.encode("ascii")).hexdigest(),
                             "seen_at": "2000-01-01T00:00:00.000000Z"} for label in labels]
                for name, item in zip(filenames, expected):
                    self.assertEqual(json.loads((self.queue / "done" / run_id / name).read_bytes()), item)
                self.assertEqual(json.loads((self.runtime / f"{run_id}.arrivals.json").read_bytes()), expected)
                report = json.loads((self.runtime / f"{run_id}.report.json").read_bytes())
                self.assertEqual(run_report.validate_run_report(report), report)
                self.assertEqual((report["run_id"], report["mode"], report["trigger"], report["arrivals"]),
                                 (run_id, "arrival", "arrival", expected))
                self.assertEqual([(item["code"], item["stage"]) for item in report["needs_g"]],
                                 [("STUB_STAGE_BACKEND", stage) for stage in run_report.STAGE_ORDER[:-1]])
                self.assertEqual((report["stages"][-1]["status"], report["stages"][-1]["error_code"]),
                                 ("skipped", "NOTIFY_PENDING"))
        self.assertEqual((self.runtime / "arrival.lock").read_bytes(), b"")
        self.assertEqual((self.runtime / "arrival.pending").read_bytes(), b"2000-01-01T00:00:00.000000Z\n")
        self.assertFalse(any(path.name in {"arrival.pending", "arrival.lock"} for path in self.queue.rglob("*")))

    def test_c2_reject_invalid_persisted_report(self):
        """C2: Do not qualify a valid in-memory run whose on-disk report is invalid."""
        write = driver.run_orchestrator.atomic_write_json

        def corrupt_report(path, data):
            if path.name.endswith(".report.json"):
                data = {**data, "unexpected": True}
            write(path, data)

        original = driver._isolated_module

        def load(name, path):
            module = original(name, path)
            if path.name == "run_orchestrator.py":
                vars(module)["atomic_write_json"] = corrupt_report
            return module

        with mock.patch.object(driver, "_isolated_module", side_effect=load):
            result = driver.run(self.root)
        self.assertEqual(result, self.expected_result(self.root, "run_report"))
        self.assertFalse((self.runtime / "arrival.pending").exists())
        self.assertFalse((self.queue / "done" / _BURST).exists())

    def test_c3_refuse_wrong_watch_directory(self):
        """C3: A miswired render cannot certify a successful downstream stamp."""
        original = driver._isolated_module

        def load(name, path):
            renderer = original(name, path)
            render = renderer.render

            def miswire(values, targets, out):
                names = render(values, targets, out)
                unit = out / "records-arrival.path"
                text = unit.read_text()
                unit.write_text(text.replace(f"PathChanged={self.queue}", f"PathChanged={self.runtime}"))
                return names

            vars(renderer)["render"] = miswire
            return renderer

        with mock.patch.object(driver, "_isolated_module", side_effect=load):
            result = driver.run(self.root)
        self.assertEqual(result, self.expected_result(self.root, "render_units"))
        self.assertFalse(self.queue.exists())

    def test_c3_stamp_write_refusal_stops_cli(self):
        """C3: Queue write denial reports exit 1 and makes no claim or report."""
        open_file = arrival.os.open

        def deny_stamp(path, flags, *args, **kwargs):
            if Path(path).parent == self.queue and flags & arrival.os.O_WRONLY:
                raise PermissionError("synthetic write denial")
            return open_file(path, flags, *args, **kwargs)

        with mock.patch.object(arrival.os, "open", side_effect=deny_stamp), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(driver.main(["--sandbox", str(self.root)]), 1)
        self.assertEqual(json.loads(out.getvalue()), self.expected_result(self.root, "write_stamp"))
        self.assertEqual(list(self.runtime.glob("*.report.json")), [])
        self.assertEqual(list((self.queue / "done").iterdir()), [])

    def test_c3_failed_claim_stops_before_report(self):
        """C3: A failed move must not be hidden by an unbound run report."""
        with mock.patch.object(arrival.os, "rename", side_effect=PermissionError):
            result = driver.run(self.root)
        self.assertEqual(result, self.expected_result(self.root, "consume"))
        self.assertTrue((self.queue / _STAMP).is_file())
        self.assertEqual(list(self.runtime.glob("*.report.json")), [])

    def test_c3_failed_second_claim_stops_before_coordination(self):
        """C3: If a burst stamp vanishes, one valid prior run cannot pass the burst."""
        real_claim = arrival.claim

        def lose_one(runtime, run_id):
            if run_id == _BURST:
                (self.queue / _SECOND).unlink()
            return real_claim(runtime, run_id)

        with mock.patch.object(arrival, "claim", side_effect=lose_one):
            result = driver.run(self.root)
        self.assertEqual(result, self.expected_result(self.root, "batch_two"))
        self.assertEqual(list(self.runtime.glob("*.report.json")), [self.runtime / f"{_SINGLE}.report.json"])
        self.assertFalse((self.runtime / "arrival.pending").exists())

    def test_c3_detect_queue_write_during_coordination(self):
        """C3: A coordination write into the watched queue is not quiet."""
        real_mark = arrival.mark_pending

        def noisy_mark(runtime):
            real_mark(runtime)
            (self.queue / "unexpected-coordination").touch()

        with mock.patch.object(arrival, "mark_pending", side_effect=noisy_mark):
            result = driver.run(self.root)
        self.assertEqual(result, self.expected_result(self.root, "coordination_quiet"))

    def test_c1_cli_creates_absent_sandbox(self):
        """C1: Success prints the whole result and returns 0 for a new directory."""
        root = self.parent / "new"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(driver.main(["--sandbox", str(root)]), 0)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(out.getvalue()), self.expected_result(root))

    def test_c3_cli_refuses_unsafe_sandbox(self):
        """C3: Refuse before marking a nonempty, nonprivate, or redirected root."""
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"unchanged")
        other = self.new_sandbox("other")
        link = self.parent / "link"
        link.symlink_to(other, target_is_directory=True)
        unsafe_mode = self.new_sandbox("unsafe-mode")
        unsafe_mode.chmod(0o755)
        for argv in [[], *(["--sandbox", str(path)] for path in
                          (self.root, sentinel, link, unsafe_mode, self.parent / "absent" / "child"))]:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    driver.main(argv)
                self.assertIs(type(caught.exception), SystemExit)
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertEqual(list(self.root.iterdir()), [sentinel])
        self.assertEqual(list(other.iterdir()), [])
        self.assertEqual(list(unsafe_mode.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
