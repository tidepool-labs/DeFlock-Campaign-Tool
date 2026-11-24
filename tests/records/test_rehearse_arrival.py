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
    "batch_two": None,
    "coordination_quiet": ["runtime/arrival.lock"],
    "arrival_after_coordination": None,
}


def _launch_once_per_stamp(text):
    start = text.index('"$TIMEOUT" -k "$KILL_AFTER" "$BACKSTOP"')
    end = text.index("\nrc=$?", start)
    return (text[:start] + 'for stamp in "$RUNTIME_DIR/arrival/done/$RUN_ID/"*.json; do\n'
            + text[start:end] + "\ndone" + text[end:])


class ArrivalRehearsalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rehearse-arrival-")
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name).resolve()
        self.root = self.new_sandbox("sandbox")
        self.runtime = self.root / "runtime"
        self.queue = self.runtime / "arrival"
        self.enterContext(mock.patch.object(arrival, "_utc_now", return_value=_CLOCK))
        self.run_child = subprocess.run
        self.wake = self.enterContext(mock.patch.object(subprocess, "run", wraps=self.run_child))
        self.socket = self.enterContext(mock.patch.object(socket, "socket", side_effect=AssertionError))

    def new_sandbox(self, name):
        root = self.parent / name
        root.mkdir(mode=0o700)
        return root

    def expected_result(self, root, failed=None, burst=None, failure_suffix="FAILED", resumed=None):
        steps = []
        codes = []
        for name, paths in _OUTPUTS.items():
            if name == failed:
                codes.append(name.upper() + "_" + failure_suffix)
                steps.append({"name": name, "code": codes[0], "outputs": []})
                break
            if name == "batch_two":
                paths = [*_files_for_claim(burst, _STAMP, _SECOND)[1:], f"runtime/{burst}.report.json",
                         f"runtime/TEST/work/arrivals-{burst}.json"]
            if name == "arrival_after_coordination":
                paths = [*_files_for_claim(resumed, _STAMP)[1:], f"runtime/{resumed}.report.json",
                         f"runtime/TEST/work/arrivals-{resumed}.json", "runtime/launches.jsonl"]
            outputs = [{"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
                       for path in sorted(paths)]
            steps.append({"name": name, "code": "OK", "outputs": outputs})
        return {"stages": ["3"], "steps": steps, "manual_steps": [], "codes": codes}

    def launches(self, root):
        trace = root / "runtime" / "launches.jsonl"
        return [json.loads(row) for row in trace.read_text().splitlines()] if trace.exists() else []

    def burst_id(self, root):
        [launch] = self.launches(root)
        self.assertEqual(launch["trigger"], "arrival")
        self.assertIs(launch["batch_bound"], True)
        return launch["run_id"]

    def mutate_wrapper(self, edit):
        original = driver._isolated_module

        def load(name, path):
            module = original(name, path)
            if path.name == "render.py":
                render = module.render

                def modified(values, targets, out):
                    names = render(values, targets, out)
                    script = out / "run-records.sh"
                    before = script.read_text()
                    after = edit(before)
                    self.assertNotEqual(before, after)
                    script.write_text(after)
                    return names

                vars(module)["render"] = modified
            return module

        return mock.patch.object(driver, "_isolated_module", side_effect=load)

    def test_c1_render_claim_and_one_report_per_wake(self):
        """C1: Claim the burst once and resume dispatch after a marker-only wake."""
        # A route to another directory, reversed stamp/claim order, or one run
        # per stamp breaks the observed service graph or the on-disk inventory.
        claim = self.enterContext(mock.patch.object(arrival, "claim", wraps=arrival.claim))
        touch = Path.touch

        def mark_first(path, **kwargs):
            self.assertEqual(list(self.root.iterdir()), [])
            touch(path, **kwargs)

        traces = []
        markers = []

        def observe_wake(*args, **kwargs):
            result = self.run_child(*args, **kwargs)
            traces.append((self.runtime / "launches.jsonl").read_bytes())
            marker = self.runtime / "arrival.pending"
            markers.append(marker.read_bytes() if marker.exists() else None)
            return result

        self.wake.side_effect = observe_wake
        with mock.patch.object(Path, "touch", autospec=True, side_effect=mark_first) as mark:
            result = driver.run(self.root)
        earlier, later = self.launches(self.root)
        burst, resumed = earlier["run_id"], later["run_id"]
        self.assertEqual(len({_SINGLE, burst, resumed}), 3)
        for launch in (earlier, later):
            self.assertEqual(launch["trigger"], "arrival")
            self.assertIs(launch["batch_bound"], True)
        self.assertEqual(result, self.expected_result(self.root, burst=burst, resumed=resumed))
        self.assertEqual(driver.STAGES, ("3",))
        self.assertIs(type(driver.TITLE), str)
        mark.assert_called_once_with(self.root / run_stages.SANDBOX_MARKER, mode=0o600, exist_ok=False)
        # Two burst wakes, a marker-only wake, then a fresh mail arrival.
        self.assertEqual([call.args[0] for call in self.wake.call_args_list],
                         [["/bin/sh", str(self.runtime / "run-records.sh"), "--arrival"]] * 4)
        first_wake, *later_wakes = self.wake.call_args_list
        self.assertTrue(all(call.kwargs == first_wake.kwargs for call in later_wakes))
        first_trace, second_trace, marker_trace, final_trace = traces
        self.assertEqual(first_trace, second_trace)
        self.assertEqual(second_trace, marker_trace)
        self.assertEqual(final_trace.splitlines(keepends=True)[:1], [marker_trace])
        self.socket.assert_not_called()
        self.assertEqual({path.name for path in self.parent.iterdir()}, {"sandbox"})
        units = self.root / "units"
        path_text = (units / "records-arrival.path").read_text()
        self.assertEqual([line for line in path_text.splitlines() if line.startswith("PathChanged=")],
                         [f"PathChanged={self.root / 'drop'}", f"PathChanged={self.queue}"])
        self.assertIn("Unit=records-arrival.service", path_text.splitlines())
        service = (units / "records-arrival.service").read_text()
        self.assertIn(f"ExecStart=/bin/sh {self.runtime / 'run-records.sh'} --arrival", service.splitlines())
        claim.assert_called_once_with(self.runtime, _SINGLE)  # Wrapper claims run in its child.
        self.assertEqual(sorted(path.name for path in self.queue.iterdir()), ["done"])
        self.assertEqual(sorted(path.name for path in (self.queue / "done").iterdir()),
                         sorted([f".{_SINGLE}.claim", f".{burst}.claim", f".{resumed}.claim",
                                 _SINGLE, burst, resumed]))
        self.assertEqual(sorted(path.name for path in self.runtime.glob("*.report.json")),
                         sorted([f"{_SINGLE}.report.json", f"{burst}.report.json", f"{resumed}.report.json"]))
        self.assertEqual((self.root / "mail-stamp.json").read_bytes(),
                         (self.queue / "done" / _SINGLE / _STAMP).read_bytes())
        for run_id, filenames, labels in [(_SINGLE, [_STAMP], ["single"]),
                                           (burst, [_SECOND, _STAMP], ["burst-two", "burst-one"]),
                                           (resumed, [_STAMP], ["after-coordination"])]:
            with self.subTest(run_id=run_id):
                self.assertEqual(json.loads((self.queue / "done" / f".{run_id}.claim").read_bytes()), filenames)
                expected = [{"source": "mail", "ref": hashlib.sha256(label.encode("ascii")).hexdigest(),
                             "seen_at": "2000-01-01T00:00:00.000000Z"} for label in labels]
                for name, item in zip(filenames, expected):
                    self.assertEqual(json.loads((self.queue / "done" / run_id / name).read_bytes()), item)
                batch_file = (self.runtime / f"{_SINGLE}.arrivals.json" if run_id == _SINGLE else
                              self.runtime / "TEST" / "work" / f"arrivals-{run_id}.json")
                self.assertEqual(json.loads(batch_file.read_bytes()), expected)
                report = json.loads((self.runtime / f"{run_id}.report.json").read_bytes())
                self.assertEqual(run_report.validate_run_report(report), report)
                self.assertEqual((report["run_id"], report["mode"], report["trigger"], report["arrivals"]),
                                 (run_id, "arrival", "arrival", expected))
                self.assertEqual([(item["code"], item["stage"]) for item in report["needs_g"]],
                                 [("STUB_STAGE_BACKEND", stage) for stage in run_report.STAGE_ORDER[:-1]])
                self.assertEqual((report["stages"][-1]["status"], report["stages"][-1]["error_code"]),
                                 ("skipped", "NOTIFY_PENDING"))
        self.assertEqual((self.runtime / "arrival.lock").read_bytes(), b"")
        self.assertEqual(markers, [None, None, b"2000-01-01T00:00:00.000000Z\n", None])
        self.assertFalse(any(path.name in {"arrival.pending", "arrival.lock"} for path in self.queue.rglob("*")))

    def test_c1_detect_launch_once_per_stamp(self):
        """Reject two launches even when they overwrite the same report path."""
        with self.mutate_wrapper(_launch_once_per_stamp):
            result = driver.run(self.root)
        first, second = self.launches(self.root)
        self.assertEqual(first, second)  # Same claimed batch, two launcher calls.
        self.assertEqual(first["trigger"], "arrival")
        self.assertEqual(arrival.pending(self.runtime), [])
        self.assertEqual(result, self.expected_result(self.root, "batch_two"))

    def test_c1_detect_launch_on_marker_only_wake(self):
        """Refuse qualification if a marker-only wake reaches the engine launcher."""
        # Model a premature launch before the empty-stamp exit. Keep real burst
        # dispatch intact and use the rendered host stub, not a fake receipt.
        def launch_on_marker(text):
            idle_exit = '  [ "$hw" -eq 0 ] || exit 0'
            return text.replace(idle_exit, '''  if [ "$hw" -eq 1 ] && [ -f "$RUNTIME_DIR/arrival.pending" ]; then
    "$HOST_PYTHON" -B -m campaign_tool.records.run_orchestrator run \\
      --run-id "$RUN_ID" --report "$REPORT" --arrival
  fi
''' + idle_exit, 1)

        with self.mutate_wrapper(launch_on_marker), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(driver.main(["--sandbox", str(self.root)]), 1,
                             "marker-only launcher regression qualified")
        result = json.loads(out.getvalue())
        self.assertEqual(result["codes"], ["COORDINATION_QUIET_FAILED"])
        self.assertEqual(result["steps"][-1], {"name": "coordination_quiet",
                                              "code": "COORDINATION_QUIET_FAILED", "outputs": []})
        self.assertTrue(all(step["code"] == "OK" for step in result["steps"][:-1]))
        burst, extra = self.launches(self.root)
        self.assertIs(burst["batch_bound"], True)
        self.assertEqual(extra["trigger"], "arrival")
        self.assertIs(extra["batch_bound"], False)
        self.assertEqual(list((self.runtime / "TEST" / "work").glob("arrivals-*.json")),
                         [self.runtime / "TEST" / "work" / f"arrivals-{burst['run_id']}.json"])

    def test_c1_empty_wakes_do_not_trigger_a_run(self):
        """An idle wake must neither launch nor qualify as a successful burst."""
        with mock.patch.object(driver, "_burst_stamps", return_value=[]):
            result = driver.run(self.root)
        self.assertEqual(self.wake.call_count, 2)
        self.assertEqual(self.launches(self.root), [])
        self.assertEqual(result, self.expected_result(self.root, "batch_two", failure_suffix="NOT_TRIGGERED"))

    def test_c1_detect_arrival_starvation_after_marker_only_wake(self):
        """A stale coordination marker must not suppress the next real stamp."""
        # Make the sandbox host report no work once the marker exists, even
        # when a real stamp is pending. The marker-only wake still looks quiet.
        dispatch = "        raise SystemExit(arrival.main(rest))"
        stub = driver._HOST_STUB.replace(dispatch, '''        if (rest[0] == "has-work" and (runtime / arrival.MARKER_NAME).is_file()):
            raise SystemExit(1)
''' + dispatch, 1)
        self.assertNotEqual(stub, driver._HOST_STUB)
        with mock.patch.object(driver, "_HOST_STUB", stub), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(driver.main(["--sandbox", str(self.root)]), 1,
                             "post-marker arrival starvation qualified")
        burst = self.burst_id(self.root)
        self.assertEqual(json.loads(out.getvalue()),
                         self.expected_result(self.root, "arrival_after_coordination", burst=burst))
        self.assertEqual([stamp["ref"] for stamp in arrival.pending(self.runtime)],
                         [hashlib.sha256(b"after-coordination").hexdigest()])

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
        self.wake.assert_not_called()

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
        def lose_one(text):
            claim = '  if ! arr claim --runtime-dir "$RUNTIME_DIR"'
            return text.replace(claim, f'  mv "$RUNTIME_DIR/arrival/{_SECOND}" "$RUNTIME_DIR/lost.json"\n' + claim, 1)

        with self.mutate_wrapper(lose_one):
            result = driver.run(self.root)
        self.assertEqual(len(self.launches(self.root)), 1)
        self.assertTrue((self.runtime / "lost.json").is_file())
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
        self.assertEqual(result, self.expected_result(self.root, "coordination_quiet", burst=self.burst_id(self.root)))

    def test_c1_cli_creates_absent_sandbox(self):
        """C1: Success prints the whole result and returns 0 for a new directory."""
        root = self.parent / "new"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(driver.main(["--sandbox", str(root)]), 0)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        burst, resumed = (launch["run_id"] for launch in self.launches(root))
        self.assertEqual(json.loads(out.getvalue()), self.expected_result(root, burst=burst, resumed=resumed))

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
