"""Synthetic evidence for the loopback console rehearsal (A3)."""

import argparse
import errno
import hashlib
import http.client
import io
import json
import os
import socket
import stat
from pathlib import Path
from threading import Event, Lock, Thread
from unittest.mock import patch

from . import admin_panel, console_check, console_export, run_stages

STAGES = ("A3",)
TITLE = "New console on loopback"
_RUN_ID = "run_20200101T000000Z_00000001"
_HOST = "127.0.0.1"
_PAGES = (
    ("/runs", "Run history"),
    ("/documents", "Documents and findings"),
    ("/findings", "All findings"),
    ("/drafts", "Draft history"),
    ("/decisions", "Decisions"),
    ("/health", "System health"),
    ("/stages", "Go-live stages"),
    ("/golive", "Go-live"),
    ("/comparison", "Policy comparison"),
)
_GOLIVE_NOTICE = b"Go-live evidence is not configured on this console."
_COMPARISON_NOTICE = b"Policy comparison: unavailable: missing. The comparison has not been written for this run yet."


def _build_export(sandbox):
    marker = sandbox / run_stages.SANDBOX_MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write('{"sandbox_marker_version":1}\n')

    project = sandbox / "project"
    run = project / "run-outputs" / _RUN_ID
    run.mkdir(parents=True, mode=0o700)
    (run / "text-state.json").write_text("{}\n", encoding="utf-8")
    build = sandbox / "build"
    build.mkdir(mode=0o700)
    # The ordinary export probes the host approval anchor even for an empty
    # project. This synthetic console has no approval trust configured.
    with patch.object(console_export.approval, "anchor_state",
                      return_value={"state": "unavailable", "reason": "synthetic_rehearsal"}):
        console_export.build(project, _RUN_ID, build)

    root = sandbox / "console"
    root.mkdir(mode=0o700)
    stream = io.BytesIO()
    console_export.write_tar(build, stream)
    stream.seek(0)
    name = console_export.install(str(root), stream)
    return root, root / "exports" / name / console_export.MANIFEST


def _empty_private(path):
    info = path.lstat()
    return (stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
            and info.st_uid == os.geteuid() and next(path.iterdir(), None) is None)


def _output(sandbox, path):
    return {"path": path.relative_to(sandbox).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _get(sandbox, port, route, heading=None):
    # No proxy, redirects, credentials, or custom Host header.
    connection = http.client.HTTPConnection(_HOST, port, timeout=3)
    try:
        connection.request("GET", route)
        with connection.getresponse() as response:
            status, body = response.status, response.read()
    finally:
        connection.close()

    receipt = sandbox / (route[1:] + "-http.json")
    receipt.write_text(json.dumps({"route": route, "status": status,
                                   "body_sha256": hashlib.sha256(body).hexdigest()}) + "\n", encoding="utf-8")
    output = _output(sandbox, receipt)
    if heading is None:
        code = "OK" if status == 404 else "SEND_NOT_DISABLED"
    elif status != 200:
        code = "PAGE_STATUS_UNEXPECTED"
    elif f"<h1>{heading}</h1>".encode() not in body:
        code = "PAGE_HEADING_MISSING"
    elif route == "/golive" and _GOLIVE_NOTICE not in body:
        # This sandbox has no ledger inputs; only certify the unconfigured route.
        code = "GOLIVE_NOTICE_MISSING"
    elif route == "/comparison" and _COMPARISON_NOTICE not in body:
        # The synthetic export has no comparison report; a heading is not enough.
        code = "COMPARISON_NOTICE_MISSING"
    else:
        code = "OK"
    return code, output


def _port_closed(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(3)
        return probe.connect_ex((_HOST, port)) == errno.ECONNREFUSED


_JOIN_TIMEOUT = 10


def run(sandbox):
    sandbox = Path(os.path.abspath(sandbox))
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}

    def step(name, code="OK", outputs=()):
        result["steps"].append({"name": name, "code": code, "outputs": list(outputs)})
        if code != "OK":
            result["codes"].append(code)
        return result

    try:
        if not _empty_private(sandbox):
            return step("build_export", "SANDBOX_INVALID")
    except OSError:
        return step("build_export", "SANDBOX_INVALID")
    try:
        root, manifest = _build_export(sandbox)
        step("build_export", outputs=[_output(sandbox, manifest)])
    except (OSError, console_export.ExportError):
        return step("build_export", "EXPORT_BUILD_FAILED")

    try:
        server = admin_panel.build_server(run_id="auto", actor="rehearsal", port=0,
                                          export_root=str(root), system=False)
    except (OSError, admin_panel.PanelError):
        return step("start_console", "CONSOLE_START_FAILED")
    with server:
        port = server.server_port
        gate = Lock()
        state = {"stopped": False, "serving": False}
        done = Event()

        def serve():
            with gate:
                if state["stopped"]:
                    done.set()
                    return
                state["serving"] = True
            try:
                server.serve_forever(poll_interval=0.05)
            finally:
                done.set()

        worker = Thread(target=serve)
        try:
            try:
                worker.start()
            except RuntimeError:
                return step("start_console", "CONSOLE_START_FAILED")
            step("start_console")
            outputs = []
            for route, heading in _PAGES:
                try:
                    code, output = _get(sandbox, port, route, heading)
                except (OSError, http.client.HTTPException):
                    return step("pages", "PAGE_PROBE_FAILED", outputs)
                outputs.append(output)
                if code != "OK":
                    return step("pages", code, outputs)
            step("pages", outputs=outputs)

            try:
                code, output = _get(sandbox, port, "/send")
            except (OSError, http.client.HTTPException):
                return step("send_disabled", "SEND_PROBE_FAILED")
            step("send_disabled", code, [output])
            if code != "OK":
                return result

            # Use only the checker's freshness check: its full CLI sends POSTs
            # and issues extra requests whose bodies it does not read.
            code = console_check._export_check(root, 36)
            if code != "PASS":
                return step("console_check", code)
            step("console_check")
        finally:
            # Entered before start(). Once `stopped` is set under the gate, the worker either already
            # set `serving` (so shutdown stops it) or sees `stopped` and returns without serving.
            with gate:
                state["stopped"] = True
                serving = state["serving"]
            if serving:
                server.shutdown()
            if serving or worker.is_alive():
                done.wait(_JOIN_TIMEOUT)
            if worker.is_alive():
                worker.join(_JOIN_TIMEOUT)

    try:
        if _port_closed(port):
            return step("stop")
    except OSError:
        pass
    return step("stop", "CONSOLE_PORT_NOT_CLOSED")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--sandbox", required=True, type=Path)
    sandbox = Path(os.path.abspath(parser.parse_args(argv).sandbox))
    try:
        sandbox.mkdir(mode=0o700, exist_ok=True)
        if not _empty_private(sandbox):
            parser.error("--sandbox must be an empty private directory (0700)")
    except OSError:
        parser.error("--sandbox must be a creatable or empty private directory (0700)")
    result = run(sandbox)
    print(json.dumps(result))
    return int(any(step["code"] != "OK" for step in result["steps"]) or bool(result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
