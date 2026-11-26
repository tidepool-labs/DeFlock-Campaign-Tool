"""Synthetic stage-4 rehearsal; not approval to run the real portal download."""

import argparse
import hashlib
import http.client
import json
import os
import socket
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
from threading import Thread

from . import rehearsal_trust, run_stages
from .ledger import store as ledger_store
from .portal.download import run_download
from .portal.engine import Limits, Response
from .portal.https_transport import EgressApproval
from .portal.policy import Approval, PortalError, url_parts
from .portal.store import Queue

STAGES = ("4",)
TITLE = "Portal downloads"

_HOST = "127.0.0.1"
_OFF_LIST = "off-list.invalid"
_DOCUMENTS = {
    "/documents/one.txt": b"Synthetic portal document one.\n",
    "/documents/two.txt": b"Synthetic portal document two.\n",
}
_REDIRECT = "/documents/redirect"
_TIMEOUT = 3


def _mark_sandbox(sandbox: Path) -> Path:
    sandbox = rehearsal_trust.sandbox_path(sandbox)
    rehearsal_trust.require_private(sandbox)
    if any(sandbox.iterdir()):
        raise ValueError("sandbox must be empty")
    marker = sandbox / run_stages.SANDBOX_MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return marker


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        body = _DOCUMENTS.get(self.path, b"")
        if self.path == _REDIRECT:
            self.send_response(302)
            self.send_header("Location", f"https://{_OFF_LIST}/documents/one.txt")
        else:
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class _Server(TCPServer):
    def get_request(self):
        conn, address = super().get_request()
        conn.settimeout(_TIMEOUT)
        return conn, address


class _LoopbackTransport:
    """Map only the logical HTTPS stub origin to the bound plaintext fixture.

    No DNS, TLS settings, proxy configuration or automatic redirects. This
    adapter never dials an address or port supplied by a URL or by policy.
    """

    def __init__(self, port):
        self.port = port

    def open(self, url, *, timeout):
        parts = url_parts(url)
        if parts.hostname != _HOST:
            raise PortalError("stub_host_mismatch")
        connection = http.client.HTTPConnection(_HOST, self.port, timeout=timeout)
        try:
            connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.sock.settimeout(timeout)
            connection.sock.connect((_HOST, self.port))
            connection.request("GET", parts.path)
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise

        def close():
            response.close()
            connection.close()

        return Response(response.status, dict(response.getheaders()),
                        iter(lambda: response.read(65536), b""), close)


def _stop_server(server, thread):
    server.shutdown()
    thread.join()


class _Rehearsal:
    def __init__(self, marker, resources):
        self.marker = marker
        self.sandbox = marker.parent
        self.resources = resources
        self.processing = self.sandbox / "processing-v1"
        self.database = self.processing / "ledger.sqlite"
        self.blobs = self.processing / "intake" / "blobs"
        self.digests = {hashlib.sha256(body).hexdigest() for body in _DOCUMENTS.values()}

    def start_stub(self):
        # TCPServer does not look up a server name when binding.
        server = self.resources.enter_context(_Server((_HOST, 0), _Handler))
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.resources.callback(_stop_server, server, thread)
        self.transport = _LoopbackTransport(server.server_address[1])
        return [self.marker]

    def policy(self):
        # In-memory policy, injected only into this driver. The real loader and
        # its admin-owned policies are deliberately not part of the rehearsal.
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        self.approval = Approval(expires, frozenset({_HOST}), frozenset(),
                                 {_HOST: ("/documents/",)})
        self.egress = EgressApproval([_HOST], expires)
        return []

    def _inventory(self, host, path):
        return self.queue.inventory(host, "synthetic-rehearsal", path.rsplit("/", 1)[1],
                                    f"https://{host}{path}",
                                    hashlib.sha256(b"Synthetic portal notice").hexdigest())

    def _download(self) -> dict:
        return run_download(self.queue, self.processing, apply=True,
                            approval_loader=lambda: self.approval,
                            egress_loader=lambda: self.egress,
                            transport=self.transport,
                            limits=Limits(max_bytes=4096, max_total_bytes=8192,
                                          max_items=2, timeout=_TIMEOUT, max_attempts=1))

    def fetch(self):
        self.blobs.mkdir(mode=0o700, parents=True)
        # initialize() sets a process-wide umask. Restore the caller's setting.
        original_umask = os.umask(0o077)
        try:
            ledger_store.initialize(self.database)
        finally:
            os.umask(original_umask)
        self.queue = Queue(self.sandbox / "portal-queue")
        self.resources.callback(self.queue.close)
        for path in _DOCUMENTS:
            self._inventory(_HOST, path)
        found = self._download()
        if (found["attempted"], found["received"], found["blocked"], found["retry"]) != (2, 2, 0, 0):
            raise PortalError("fixture_fetch_incomplete")
        if {receipt["sha256"] for receipt in found["receipts"]} != self.digests:
            raise PortalError("fixture_digest_mismatch")
        self.delivery = found["ledger_delivered"], found["ledger_pending"]
        return [self.queue.objects / digest for digest in sorted(self.digests)]

    def ledger(self):
        with ledger_store.ledger(self.database, readonly=True) as con:
            rows = {tuple(row) for row in con.execute(
                "SELECT portal_host, original_sha256, retrieval_status FROM portal_items")}
        expected = {(_HOST, digest, "bytes_verified_stage_pending") for digest in self.digests}
        if self.delivery != (2, 0) or rows != expected:
            raise PortalError("fixture_enrollment_incomplete")
        blobs = [self.blobs / digest for digest in sorted(self.digests)]
        for blob in blobs:
            if ledger_store.file_hash(blob) != blob.name:
                raise PortalError("fixture_enrollment_digest_mismatch")
        return [self.database, *blobs]

    def _refusal(self, host, path, reason, http_status):
        item = self._inventory(host, path)
        found = self._download()
        if (found["attempted"], found["received"], found["blocked"], found["retry"],
                found["ledger_delivered"], found["ledger_pending"]) != (1, 0, 1, 0, 0, 0):
            raise PortalError("expected_refusal_missing")
        if found["receipts"] != [{"queue_item_id": item, "sha256": None, "host": host,
                                  "bytes": 0, "status": "blocked", "reason": reason,
                                  "http_status": http_status}]:
            raise PortalError("unexpected_refusal")
        return []

    def off_list_refused(self):
        return self._refusal(_OFF_LIST, "/documents/one.txt", "portal_not_approved", None)

    def redirect_refused(self):
        return self._refusal(_HOST, _REDIRECT, "redirect_not_approved", 302)

    def stop_stub(self):
        self.resources.close()
        return [self.queue.database]


def run(sandbox):
    """Exercise the fetch and ledger paths on invented inputs in an empty sandbox."""
    marker = _mark_sandbox(sandbox)
    steps, codes = [], []
    with ExitStack() as resources:
        rehearsal = _Rehearsal(marker, resources)
        for step in (rehearsal.start_stub, rehearsal.policy, rehearsal.fetch, rehearsal.ledger,
                     rehearsal.off_list_refused, rehearsal.redirect_refused, rehearsal.stop_stub):
            try:
                outputs = [{"path": path.relative_to(marker.parent).as_posix(),
                            "sha256": ledger_store.file_hash(path)} for path in step()]
            except (OSError, PortalError, RuntimeError, sqlite3.Error, ValueError):
                code = step.__name__.upper() + "_FAILED"
                steps.append({"name": step.__name__, "code": code, "outputs": []})
                codes.append(code)
                break
            steps.append({"name": step.__name__, "code": "OK", "outputs": outputs})
    return {"stages": list(STAGES), "steps": steps, "manual_steps": [], "codes": codes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path, required=True, help="empty owner-only 0700 directory")
    args = parser.parse_args(argv)
    try:
        report = run(args.sandbox)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(report))
    return int(bool(report["manual_steps"]) or any(step["code"] != "OK" for step in report["steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
