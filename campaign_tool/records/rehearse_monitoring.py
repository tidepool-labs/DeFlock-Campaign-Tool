"""Rehearse stage 18 with synthetic inputs; never run any rendered unit.

Use an empty private sandbox outside a Git checkout (the renderer's normal
output restriction). The health check reads synthetic notify status, but the
notify stage, fallback and transport are never called.
"""
import argparse
import hashlib
import json
import runpy
import stat
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import pipeline_health, run_report, run_stages, schedule, schedule_snapshot

STAGES = ("18",)
TITLE = "Rehearse monitoring timers and pipeline-health alerts"
_RUNTIME_ASSETS = Path(__file__).resolve().parents[2] / "deploy/records-runtime"
_TARGET = "TEST"
_OUTPUTS_CODE = "OUTPUTS_SYNTHETIC_FAILURE"
_CLOCK = datetime(2025, 1, 8, 23, tzinfo=timezone.utc)


def _render_units(sandbox):
    out = sandbox / "units"
    values = {
        "ENGINE_DIR": "engine", "DOCKER": "bin/docker", "TIMEOUT": "bin/timeout", "FLOCK": "bin/flock",
        "RUNTIME_DIR": "runtime", "HOST_PYTHON": "bin/python", "NOTIFY_PY": "inert/notify.py",
        "NOTIFY_ENV_FILE": "inert/notify.env", "RUN_RECORDS_SH": "units/run-records.sh",
        "RECORDS_DROP_DIR": "drop", "RECORDS_ROOT": "records", "STUB_FIXTURE": "inert/stub.json",
        "CONTAINER_ENGINE_DIR": "engine", "CONTAINER_PYTHON": "bin/python", "CONTAINER_PROJECT_DIR": "project",
        "BACKUP_DIR": "backups",
    }
    values = {key: str(sandbox / path) for key, path in values.items()}
    values.update(RELEASE_COMMIT="a" * 40, EXPECTED_IMAGE="sha256:" + "b" * 64,
                  CONTAINER="synthetic", CONTAINER_USER="synthetic", RUN_UID="1000", NOTIFY_LABEL="Synthetic",
                  DOCKER_WAIT_ATTEMPTS="1", DOCKER_WAIT_DELAY="1", DOCKER_WAIT_MAX_DELAY="1",
                  DOCKER_CMD_TIMEOUT="1", DOCKER_WAIT_DEADLINE="10", NOTIFY_TIMEOUT="1",
                  RUN_BUDGET_SECONDS="120", MIN_FREE_BYTES="67108864", DEFAULT_TARGET=_TARGET,
                  SCHEDULED_TARGET=_TARGET)
    # The standalone renderer prepends its checkout to sys.path on load.
    original_path = sys.path[:]
    try:
        render = runpy.run_path(str(_RUNTIME_ASSETS / "render.py"))["render"]
    finally:
        sys.path[:] = original_path
    written = render(values, [(_TARGET, _RUNTIME_ASSETS / "targets/stub.json.in")], out)
    for name, calendar in (("records-health.timer", "hourly"), ("records-backup-check.timer", "*-*-* 06:10")):
        lines = (out / name).read_text(encoding="utf-8").splitlines()
        if [line for line in lines if line.startswith("OnCalendar=")] != [f"OnCalendar={calendar}"]:
            raise ValueError("unexpected timer calendar")
    return [out / name for name in sorted(written)]


def _write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _add_report(reports, root, finished, outputs_code):
    started = finished - timedelta(minutes=1)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    run_id = started.strftime("run_%Y%m%dT%H%M%SZ_00000000")
    stages = []
    for name in run_report.STAGE_ORDER:
        code = outputs_code if name == "outputs" else None
        status = "skipped" if name == "notify" else "warn" if code else "ok"
        stages.append({"name": name, "status": status, "error_code": code,
                       "duration_seconds": None if status == "skipped" else 1,
                       "counts": dict.fromkeys(("input", "output", "failed", "skipped"), 0)})
    report = {"schema_version": 1, "report_kind": "flock-records-run-report", "run_id": run_id,
              "mode": "scheduled", "started_at": started.strftime(stamp), "finished_at": finished.strftime(stamp),
              "duration_seconds": 60, "overall_status": "warn" if outputs_code else "ok", "stages": stages,
              "new_data": dict.fromkeys(("documents", "pages", "extracted_units", "portal_releases",
                                         "request_status_changes"), 0),
              "review_required_count": 0, "notify_failed": False, "needs_g": []}
    run_report.validate_run_report(report)
    _write_json(reports / (run_id + ".json"), report)
    _write_json(reports.parent / "run-report.json", report)
    outcomes = root / "run-outputs" / run_id / "outcomes"
    outcomes.mkdir(parents=True)
    _write_json(outcomes / "release-links.json", {"schema": pipeline_health.RELEASE_LINKS_SCHEMA,
                                                  "run_id": run_id, "links": []})


def _observe(sandbox, name, now, finishes, outputs_code, expected_state, expected_alerts, refresh_poll):
    runtime = sandbox / "runtime"
    target = runtime / _TARGET
    root = sandbox / "records"
    reports = target / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    for finished in finishes:
        _add_report(reports, root, finished, outputs_code)
    if refresh_poll:
        poll = runtime / "mail-poll"
        poll.mkdir(exist_ok=True)
        _write_json(poll / "state.json", {"updated_at": now.isoformat()})
    spec = schedule.DEFAULT_SCHEDULE
    # Keep schedule health independent of the run report and mail poll.
    snapshot = target / "schedule-snapshot.json"
    _write_json(snapshot, {"schema": schedule_snapshot.SCHEMA, "generated_at": now.isoformat(),
                           "runs": [{"started_at": slot.isoformat(), "ended_at": slot.isoformat(),
                                     "status": "completed"} for slot in schedule.due_slots(now, 3, spec)]})
    report = pipeline_health.assess(runtime_dir=runtime, target=_TARGET, records_root=root, mail_store=runtime,
                                    now=now, spec=spec, schedule_snapshot_path=snapshot, check_disk=False)
    alerts = target / "alerts"
    pipeline_health.write_alerts(report, alerts)
    observation = sandbox / name
    observation.mkdir()
    health = observation / "health.json"
    _write_json(health, report)
    outputs = [health]
    actual_alerts = {}
    # Retain the actual writer bytes, not links to files the next step clears.
    for alert in sorted(alerts.iterdir()):
        body = alert.read_bytes()
        data = json.loads(body)
        actual_alerts[alert.name] = (data["state"], data.get("code"))
        copy = observation / alert.name
        copy.write_bytes(body)
        outputs.append(copy)
    checks = {item["check"]: item for item in report["checks"]}
    mail = checks.get("mail_poll", {})
    expected_mail = ("ok", "mail_poll_recent") if refresh_poll else ("warn", "mail_poll_stale")
    mail_matches = (mail.get("state"), mail.get("reason_code")) == expected_mail
    # Mail loss and recovery must both be isolated from other configured check failures.
    peers_required = not refresh_poll or name == "mail_poll_recovered"
    peers_healthy = not peers_required or all(
        item["state"] == "ok" or item["reason_code"] in pipeline_health.NOT_CONFIGURED
        for check, item in checks.items() if check != "mail_poll"
    )
    return (report["state"] == expected_state and actual_alerts == expected_alerts
            and mail_matches and peers_healthy), outputs


def run(sandbox):
    """Run the steps in an existing empty 0700 directory, stopping on error."""
    info = sandbox.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or any(sandbox.iterdir()):
        raise ValueError("sandbox must be an empty mode-0700 directory")
    sandbox = sandbox.resolve()
    (sandbox / run_stages.SANDBOX_MARKER).touch(mode=0o600, exist_ok=False)
    stale_clock = _CLOCK + timedelta(hours=pipeline_health.LIMITS["run_max_hours"], minutes=1)
    recovered_clock = stale_clock + timedelta(minutes=2)
    # Advance past the warning threshold without refreshing the last good poll.
    mail_stale_clock = recovered_clock + timedelta(hours=pipeline_health.LIMITS["mail_poll_warn_hours"], minutes=1)
    streak = pipeline_health.LIMITS["outputs_streak"]
    stale = "health-last_run-no_recent_run"
    failed = "health-outputs-outputs_degraded"
    mail = "health-mail_poll-mail_poll_stale"
    stale_cleared = {stale + ".cleared.json": ("cleared", None)}
    recovered_alerts = {**stale_cleared, failed + ".cleared.json": ("cleared", _OUTPUTS_CODE)}
    scenarios = (
        ("healthy", _CLOCK, [_CLOCK - timedelta(minutes=n) for n in range(streak, 0, -1)], None, "ok", {}, True),
        ("stale_run", stale_clock, (), None, "stop", {stale + ".json": ("stop", None)}, True),
        ("failed_outputs", stale_clock + timedelta(minutes=1),
         [stale_clock - timedelta(seconds=n) for n in range(streak, 0, -1)], _OUTPUTS_CODE, "warn",
         {**stale_cleared, failed + ".json": ("warn", _OUTPUTS_CODE)}, True),
        ("recovered", recovered_clock, [stale_clock + timedelta(minutes=1)], None, "ok", recovered_alerts, True),
        ("mail_poll_stale", mail_stale_clock, [mail_stale_clock - timedelta(minutes=1)], None, "warn",
         {**recovered_alerts, mail + ".json": ("warn", None)}, False),
        ("mail_poll_recovered", mail_stale_clock + timedelta(minutes=1), (), None, "ok",
         {**recovered_alerts, mail + ".cleared.json": ("cleared", None)}, True),
    )
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    steps: list[tuple[str, Callable[[], tuple[bool, list[Path]]]]] = [
        ("render_units", lambda: (True, _render_units(sandbox))),
    ]
    steps.extend((scenario[0], lambda scenario=scenario: _observe(sandbox, *scenario)) for scenario in scenarios)
    for name, step in steps:
        try:
            matched, paths = step()
            outputs = [{"path": path.relative_to(sandbox).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths]
        except (OSError, ValueError, pipeline_health.HealthError):
            matched, outputs = False, []
        code = "OK" if matched else name.upper() + "_FAILED"
        result["steps"].append({"name": name, "code": code, "outputs": outputs})
        if not matched:
            result["codes"].append(code)
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--sandbox", required=True, type=Path)
    sandbox = parser.parse_args(argv).sandbox
    try:
        if sandbox.is_symlink():
            raise ValueError
        sandbox.mkdir(mode=0o700, exist_ok=True)
        if any(sandbox.iterdir()):
            raise ValueError
        sandbox.chmod(0o700)
    except (OSError, ValueError):
        parser.error("--sandbox requires a new or empty directory")
    result = run(sandbox)
    print(json.dumps(result, sort_keys=True))
    return int(any(step["code"] != "OK" for step in result["steps"]) or bool(result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
