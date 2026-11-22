"""Rehearse arrival dispatch without starting a unit, wrapper, or live stage."""

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

from . import arrival, run_orchestrator, run_report, run_stages

STAGES = ("3",)
TITLE = "Mail arrival triggers one records run"
_KIT = run_report.REPO_ROOT / "deploy" / "records-runtime"
_SINGLE = "run_20000101T000000Z_00000001"
_BATCH = "run_20000101T000000Z_00000002"
_NO_WORK = "run_20000101T000000Z_00000003"


class _Failed(ValueError):
    """A rehearsal invariant or renderer check failed."""


def _require(condition):
    if not condition:
        raise _Failed


def _sandbox_root(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid() or any(path.iterdir())):
        raise ValueError("sandbox must be an empty owner-only directory")
    return path.resolve(strict=True)


def _isolated_module(name, path):
    # Local globals let us disable the optional host analyzer and replace only
    # the stage transport, without changing the imported modules for other drivers.
    # Compile directly so this load never creates bytecode beside shipped assets.
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = name.rpartition(".")[0]
    exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
    return module


def _render(root, runtime, units, fixture):
    (root / run_stages.SANDBOX_MARKER).touch(mode=0o600, exist_ok=False)
    runtime.mkdir(mode=0o700)
    drop = root / "drop"
    drop.mkdir(mode=0o700)
    fixture.write_text('{"stub_fixture_version": 1}\n', encoding="ascii")

    # render.py adds the checkout to sys.path when loaded; the driver already
    # imports from that checkout. Keep the caller's search path unchanged.
    search_path = sys.path[:]
    try:
        renderer = _isolated_module("_arrival_renderer", _KIT / "render.py")
    finally:
        sys.path[:] = search_path
    # Use the renderer's built-in static timer checks, not systemd-analyze.
    vars(renderer)["shutil"] = SimpleNamespace(which=lambda _: None)
    wrapper = runtime / "run-records.sh"
    values = {
        "RELEASE_COMMIT": "0" * 40,
        "ENGINE_DIR": str(root / "unused-engine"),
        "DOCKER": str(root / "unused-docker"),
        "TIMEOUT": str(root / "unused-timeout"),
        "FLOCK": str(root / "unused-flock"),
        "CONTAINER": "synthetic-records",
        "EXPECTED_IMAGE": "sha256:" + "0" * 64,
        "RUN_UID": str(os.getuid()),
        "RUNTIME_DIR": str(runtime),
        "HOST_PYTHON": str(root / "unused-python"),
        "NOTIFY_PY": str(root / "unused-notify.py"),
        "NOTIFY_ENV_FILE": str(root / "unused-notify-env"),
        "NOTIFY_LABEL": "Synthetic",
        "DOCKER_WAIT_ATTEMPTS": "3",
        "DOCKER_WAIT_DELAY": "0",
        "DOCKER_WAIT_MAX_DELAY": "0",
        "DOCKER_CMD_TIMEOUT": "1",
        "DOCKER_WAIT_DEADLINE": "30",
        "NOTIFY_TIMEOUT": "30",
        "RUN_BUDGET_SECONDS": "600",
        "DEFAULT_TARGET": "TEST",
        "SCHEDULED_TARGET": "TEST",
        "RUN_RECORDS_SH": str(wrapper),
        "RECORDS_DROP_DIR": str(drop),
        "STUB_FIXTURE": str(fixture),
        "MIN_FREE_BYTES": "67108864",
        "RECORDS_ROOT": str(root / "unused-records"),
    }
    try:
        names = renderer.render(values, [("TEST", _KIT / "targets" / "stub.json.in")], units)
    except renderer.RenderError:
        raise _Failed from None
    path_lines = (units / "records-arrival.path").read_text(encoding="utf-8").splitlines()
    _require({line for line in path_lines if line.startswith("PathChanged=")}
             == {f"PathChanged={drop}", f"PathChanged={runtime / 'arrival'}"})
    _require("Unit=records-arrival.service" in path_lines)
    service = (units / "records-arrival.service").read_text(encoding="utf-8").splitlines()
    _require([line for line in service if line.startswith("ExecStart=")]
             == [f"ExecStart=/bin/sh {wrapper} --arrival"])
    return [root / run_stages.SANDBOX_MARKER, fixture, *(units / name for name in names)]


def _mail_stamp(runtime, label):
    # Same arrival writer the mail hook invokes, with a synthetic opaque ref.
    ref = hashlib.sha256(label.encode("ascii")).hexdigest()
    path = runtime / "arrival" / arrival.stamp(runtime, "mail", ref)
    stamp = arrival.validate_stamp(json.loads(path.read_bytes()))
    _require(stamp["source"] == "mail" and stamp["ref"] == ref)
    return path, stamp


def _claim(runtime, run_id, stamps):
    if not arrival.has_work(runtime, stamps_only=True):
        return []
    batch = arrival.claim(runtime, run_id)
    expected = sorted(stamps, key=lambda pair: pair[0].name)
    _require(batch == [stamp for _, stamp in expected] and not arrival.pending(runtime))
    claimed = runtime / "arrival" / "done" / run_id
    paths = sorted(claimed.iterdir())
    _require([path.name for path in paths] == [path.name for path, _ in expected])
    for moved, (original, stamp) in zip(paths, expected):
        _require(not original.exists() and json.loads(moved.read_bytes()) == stamp)
    batch_file = runtime / f"{run_id}.arrivals.json"
    run_orchestrator.atomic_write_json(batch_file, batch)
    return [batch_file, *paths, claimed.parent / f".{run_id}.claim"]


def _report(runtime, target, fixture, run_id, batch_file):
    orch = _isolated_module(f"{__package__}._arrival_orchestrator", Path(run_orchestrator.__file__))

    def run_stub(argv, timeout, schema, work_dir, env):
        # Never execute argv. Only the exact shipped TEST stub is admitted;
        # keep the real stage schema parser and run/report assembly in use.
        _require(len(argv) == 9)
        stage = argv[6]
        _require(stage in orch.WORK_STAGES and argv == [
            str(runtime.parent / "unused-python"), "-B", "-m",
            "campaign_tool.records.run_orchestrator", "stub-stage", "--stage",
            stage, "--fixture", str(fixture),
        ])
        _require(env.get("RECORDS_RUN_TRIGGER") == "arrival")
        lines = []
        vars(orch)["print"] = lines.append
        _require(orch.stub_stage(stage, fixture) == 0)
        result = orch.parse_stage_result("\n".join(lines).encode("utf-8"), schema)
        _require(result is not None)
        return result["status"], result["counts"], result["error_code"], result, 0

    vars(orch)["run_stage"] = run_stub
    config = orch.load_target_config(target)
    _require(config["target"] == "TEST" and config["mode"] == "test")
    arrivals = orch.load_arrivals(batch_file)
    mode = orch.report_mode(config, scheduled=False, arrival=True)
    trigger = orch.report_trigger(mode, scheduled=False)
    path = runtime / f"{run_id}.report.json"
    rc = orch.execute(config, run_id, mode, orch.utc_now(), time.time() + 60,
                      path, runtime, env={}, trigger=trigger, arrivals=arrivals)
    report = run_report.validate_run_report(json.loads(path.read_bytes()))
    _require(rc == 0 and report["overall_status"] == "ok" and report["run_id"] == run_id
             and report["mode"] == report["trigger"] == "arrival" and report["arrivals"] == arrivals)
    return path


def run(sandbox):
    """Run the ordered offline rehearsal in a caller-owned empty 0700 sandbox."""
    root = _sandbox_root(sandbox)
    runtime, units, fixture = root / "runtime", root / "units", root / "stub.json"
    queue = runtime / "arrival"
    target = units / "targets" / "TEST.json"
    lock = runtime / arrival.LOCK_NAME

    def write_stamp():
        _require(not lock.exists() and not arrival.has_work(runtime))
        first = _mail_stamp(runtime, "single")
        _require(lock.is_file() and arrival.pending(runtime) == [first[1]])
        # Retain the pre-claim bytes so every output digest is verifiable after
        # the queue entry is moved in the next step.
        snapshot = root / "mail-stamp.json"
        snapshot.write_bytes(first[0].read_bytes())
        stamps.append(first)
        return [snapshot, lock]

    def consume():
        claim = _claim(runtime, _SINGLE, stamps)
        _require(bool(claim))
        return claim

    def report():
        return [_report(runtime, target, fixture, _SINGLE, runtime / f"{_SINGLE}.arrivals.json")]

    def batch_two():
        pair = [_mail_stamp(runtime, label) for label in ("burst-one", "burst-two")]
        _require(len(arrival.pending(runtime)) == 2)
        existing_reports = set(runtime.glob("*.report.json"))
        claim = _claim(runtime, _BATCH, pair)
        _require(bool(claim))
        report_path = _report(runtime, target, fixture, _BATCH, claim[0])
        # A second wake after the burst was consumed has no batch to dispatch.
        _require(_claim(runtime, _NO_WORK, []) == [])
        _require(set(runtime.glob("*.report.json")) - existing_reports == {report_path})
        return [*claim, report_path]

    def coordination_quiet():
        marker = runtime / arrival.MARKER_NAME
        _require(not marker.exists())
        before = queue.stat().st_mtime_ns, set(queue.rglob("*"))
        inode = lock.stat().st_ino
        arrival.mark_pending(runtime)
        _require(lock.stat().st_ino == inode and arrival.has_work(runtime)
                 and not arrival.has_work(runtime, stamps_only=True)
                 and before == (queue.stat().st_mtime_ns, set(queue.rglob("*"))))
        arrival.parse_seen_at(marker.read_text(encoding="ascii").strip())
        return [lock, marker]

    stamps = []
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    for name, action in (
        ("render_units", lambda: _render(root, runtime, units, fixture)),
        ("write_stamp", write_stamp), ("consume", consume), ("run_report", report),
        ("batch_two", batch_two), ("coordination_quiet", coordination_quiet),
    ):
        try:
            outputs = [{"path": path.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                       for path in sorted(action())]
        except (OSError, ValueError, arrival.ArrivalError):
            code = name.upper() + "_FAILED"
            result["steps"].append({"name": name, "code": code, "outputs": []})
            result["codes"].append(code)
            break
        result["steps"].append({"name": name, "code": "OK", "outputs": outputs})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        args.sandbox.mkdir(mode=0o700, exist_ok=True)
        result = run(args.sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must name an absent or empty owner-only directory")
    print(json.dumps(result))
    return int(bool(result["manual_steps"] or any(step["code"] != "OK" for step in result["steps"])))


if __name__ == "__main__":
    raise SystemExit(main())
