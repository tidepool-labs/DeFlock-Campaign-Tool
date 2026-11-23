"""Offline runtime contracts for the stage 16 rehearsal (analysis lane to one pending finding packet)."""

from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import analysis_bridge, analysis_export, analysis_packets, console_findings, console_packets
from campaign_tool.records import finding_review, public_packets, rehearsal_trust
from campaign_tool.records import rehearse_findings as driver
from campaign_tool.records.run_stages import SANDBOX_MARKER

RUN = "run_20261102T180000Z_5ced0016"
AGENCY, SLUG, REQUEST, FINDING = "synthetic-agency", "synthetic-agency-police", "SYN-REQ-16", "SYN-F1"
TOPIC, CLAIM = "Synthetic rehearsal topic", "Synthetic rehearsal claim for the stage 16 sandbox."
PAGES = ("Synthetic record page one.", "Synthetic record page two.")
DIGEST = hashlib.sha256(("SYNTHETIC STAGE 16 RECORD\n" + "\n".join(PAGES)).encode()).hexdigest()
FACT_ID = "F-" + hashlib.sha256("\x1f".join((SLUG, DIGEST, "2", "synthetic_observation", "synthetic value")).encode()).hexdigest()[:12]
SOURCE = "fr-" + hashlib.sha256((SLUG + "\x1f" + FINDING).encode()).hexdigest()[:24]
DRAFT = "\n\nResearch draft - not legal advice. The us-ca law package is draft and unreviewed."  # the real lane adds it for a draft law package
PACKET = {"kind": "finding", "agency_key": AGENCY, "request_id": REQUEST, "source": SOURCE, "title": TOPIC, "text": CLAIM + DRAFT,
          "documents": [{"sha256": DIGEST, "date_received": "2026-11-02", "source_request": REQUEST, "redaction_ids": [],
                         "use_restriction": False}],
          "citations": [{"ref": "record %s page 2 (%s)" % (DIGEST[:12], FACT_ID), "verified": True}],
          "finding_state": public_packets.PUBLIC_STATE}
PACKET_ID = hashlib.sha256(json.dumps(["finding", AGENCY, REQUEST, SOURCE], sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
REVIEW = "analysis/agencies/%s/review" % SLUG
STEPS = ("seed", "analysis_sync", "finding_review", "packet_stage", "packet_import", "packet_state", "console_findings")
PRIVATE_TEXT = (TOPIC, CLAIM, *PAGES)


class Base(unittest.TestCase):
    def setUp(self):
        # Use the physical temp root, as the records readers reject symlink ancestry.
        root = self.enterContext(tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()))
        self.root = Path(root)
        self.sandbox = self.root / "sandbox"
        self.project = self.sandbox / "project"
        self.enterContext(mock.patch("socket.socket", side_effect=AssertionError("unexpected socket.socket")))
        real_run = subprocess.run

        def only_ssh_keygen(argv, *args, **kwargs):  # the sandbox signer and verifier are the only commands the rehearsal may run
            if argv[0] != str(approval.SSH_KEYGEN):
                raise AssertionError(f"unexpected command {argv[0]}")
            return real_run(argv, *args, **kwargs)
        self.enterContext(mock.patch("subprocess.run", side_effect=only_ssh_keygen))
        for name in ("approve", "decide", "enable", "write_change"):  # nothing is approved, decided, enabled or written
            self.enterContext(mock.patch.object(public_packets, name, side_effect=AssertionError(f"unexpected {name}")))

    def run_driver(self):
        self.sandbox.mkdir(mode=0o700)
        return driver.run(self.sandbox)

    def outputs(self):
        config = json.loads((self.sandbox / "analysis/agencies" / SLUG / "review-config.json").read_text())
        run = Path(config["compare"]["f3"]["store"]).relative_to(self.sandbox) / "agencies" / SLUG / "runs" / config["review"]["run_date"]
        (sent,) = (self.sandbox / "analysis/.analysis/packet-outbox/sent").glob("*.json")
        journal, receipt = "project/public-packets.jsonl", "project/analysis-packets-imported.jsonl"
        return {
            "seed": [SANDBOX_MARKER, "analysis/profiles/%s.json" % SLUG, f"project/run-outputs/{RUN}/outcomes/analysis-ready.json",
                     f"project/run-outputs/{RUN}/outcomes/received-items.json", f"export/{RUN}/EXPORT.json"],
            "analysis_sync": [f"analysis/.analysis/runs/{RUN}/EXPORT.json", f"{REVIEW}/inputs/SOURCES.csv",
                              f"analysis/agencies/{SLUG}/review-config.json", "analysis/.analysis/analysis-ledger.jsonl"],
            "finding_review": [f"{REVIEW}/facts/store/agencies/{SLUG}/FACTS.csv", f"{REVIEW}/facts/store/agencies/{SLUG}/FACT-REVIEWS.csv",
                               f"{run}/FINDINGS.csv", f"{run}/PUBLIC-OK.csv", "rehearsal.allowed_signers",
                               "analysis/.analysis/finding-reviews.jsonl"],
            "packet_stage": [],
            "packet_import": [sent.relative_to(self.sandbox).as_posix(), journal, receipt],
            "packet_state": [journal, receipt], "console_findings": [journal]}

    def expected(self):
        paths = self.outputs()
        steps = [dict(name=name, code="OK", outputs=[
            dict(path=rel, sha256=hashlib.sha256((self.sandbox / rel).read_bytes()).hexdigest()) for rel in paths[name]])
            for name in STEPS]
        return dict(stages=["16"], steps=steps, manual_steps=[], codes=[])

    def failed(self, failure_at, code):
        """The report of a run stopped at `failure_at`: the earlier steps are whole and OK, nothing later was tried."""
        report = self.run_driver()
        self.assertEqual([s["name"] for s in report["steps"]], list(STEPS[:STEPS.index(failure_at) + 1]))
        self.assertEqual(report["steps"][-1], dict(name=failure_at, code=code, outputs=[]))
        self.assertEqual((report["codes"], report["manual_steps"], report["stages"]), ([code], [], ["16"]))
        self.assertTrue(all(s["code"] == "OK" for s in report["steps"][:-1]))
        return report

    def journal(self):
        return [json.loads(line) for line in (self.project / "public-packets.jsonl").read_bytes().splitlines()]


class C1Steps(Base):
    def test_c1_every_step_runs_against_the_real_lane(self):
        """C1: seed, analysis_sync, packet_import, packet_state and console_findings all pass, with these exact outputs."""
        real_seed = driver._seed_project

        def seed(p):
            marker = self.sandbox / SANDBOX_MARKER  # the marker is written before any other input
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual(sorted(x.name for x in self.sandbox.iterdir()), sorted([SANDBOX_MARKER, "analysis", "tmp", "export"]))
            return real_seed(p)

        with mock.patch.object(driver, "_seed_project", side_effect=seed), \
                mock.patch.object(analysis_packets, "main", wraps=analysis_packets.main) as imported:
            report = self.run_driver()
        self.assertEqual(report, self.expected())
        self.assertEqual(imported.call_args_list, [mock.call(["--project", str(self.project), "--from",
                                                              str(self.sandbox / "analysis/.analysis/packet-outbox")])])
        self.assertEqual(os.listdir(self.sandbox / "analysis/.analysis/packet-outbox"), ["sent"])  # moved as run-analysis.sh does

    def test_c1_seed_exports_one_record_for_the_profiled_agency(self):
        """C1: the real analysis-export wrote one exported record, and the profile names that agency."""
        self.run_driver()
        export = self.sandbox / "export" / RUN
        self.assertEqual(sorted(p.relative_to(export).as_posix() for p in export.rglob("*") if p.is_file()),
                         ["EXPORT.json", "TEXTS.csv", "analysis-ready.json", f"texts/{DIGEST}.txt"])
        text = (export / "texts" / (DIGEST + ".txt")).read_bytes()
        self.assertEqual(text.decode(), "[[PAGE 1]]\n%s\n[[PAGE 2]]\n%s\n" % PAGES)
        self.assertEqual((export / "TEXTS.csv").read_text().splitlines()[1].split(",")[:6],
                         [DIGEST, AGENCY, REQUEST, "2", hashlib.sha256(text).hexdigest(), "exported"])
        profile = json.loads((self.sandbox / "analysis/profiles" / (SLUG + ".json")).read_text())
        self.assertEqual((profile["records_agency_id"], profile["slug"]), (AGENCY, SLUG))

    def test_c1_analysis_sync_places_the_record_as_a_review_input(self):
        """C1: receive then replay leaves one synced pair and the record text as the agency's review input."""
        self.run_driver()
        ledger = [json.loads(x) for x in (self.sandbox / "analysis/.analysis/analysis-ledger.jsonl").read_text().splitlines()]
        self.assertEqual([(e["kind"], e["run_id"], e.get("agency_id"), e["outcome"]) for e in ledger],
                         [("receive", RUN, None, "RECEIVED"), ("receive", RUN, None, "EXPORT_CONFLICT"),
                          ("sync", RUN, AGENCY, "SYNCED")])
        self.assertEqual((self.sandbox / REVIEW / "inputs/records" / (DIGEST + ".txt")).read_text(),
                         "[[PAGE 1]]\n%s\n[[PAGE 2]]\n%s\n" % PAGES)

    def test_c1_packet_import_proposes_exactly_one_finding_packet(self):
        """C1: the import proposes one finding packet at revision 0 and receipts it once."""
        self.run_driver()
        (event,) = self.journal()
        self.assertEqual({k: v for k, v in event.items() if k not in ("ts", "at")},
                         {"schema": public_packets.SCHEMA, "seq": 0, "event": "packet", "id": PACKET_ID, "revision": 0,
                          "packet": public_packets.clean(PACKET),
                          "digest": hashlib.sha256(json.dumps(public_packets.clean(PACKET), sort_keys=True,
                                                              separators=(",", ":")).encode()).hexdigest()})
        (receipt,) = [json.loads(x) for x in (self.project / "analysis-packets-imported.jsonl").read_text().splitlines()]
        (sent,) = (self.sandbox / "analysis/.analysis/packet-outbox/sent").glob("*.json")
        self.assertEqual(receipt, {"key": json.loads(sent.read_text())["key"], "source": SOURCE, "result": "proposed r0"})

    def test_c1_packet_state_is_pending_unsigned_switch_off_and_unwritten(self):
        """C1: the one packet is proposed, unsigned and unapproved; the switch is OFF; nothing is written for the site."""
        self.run_driver()
        (event,) = self.journal()
        ts = event["ts"]
        self.assertEqual(public_packets.pending(str(self.project), now=ts, read_only=True), [{
            "id": PACKET_ID, "revision": 0, "status": "proposed", "kind": "finding", "agency_key": AGENCY, "title": TOPIC,
            "request_id": REQUEST, "source": SOURCE, "proposed_at": public_packets._iso(ts), "age_hours": 0,
            "expires_at": public_packets._iso(ts + public_packets.EXPIRY_SECONDS), "hours_to_expiry": 336,
            "approved_at": None, "signed_by": None}])
        self.assertEqual(public_packets.status(str(self.project), now=ts, read_only=True), {
            "switch": "OFF", "packets": [{"id": PACKET_ID, "revision": 0, "status": "proposed", "kind": "finding",
                                          "agency_key": AGENCY, "title": TOPIC}]})
        for name in (public_packets.OUT, public_packets.SWITCH, public_packets.KILL):
            self.assertFalse(os.path.lexists(self.project / name), name)

    def test_c1_console_lists_the_packet_and_the_source_finding(self):
        """C1: /packets lists the one pending packet read-only; /findings lists the source finding, not the packet."""
        claim = CLAIM + " <sample> &lt;"  # catches both missing and repeated HTML unescaping
        with mock.patch.object(driver, "_CLAIM", claim):
            report = self.run_driver()
        self.assertEqual(report, self.expected())
        handler = driver._Handler("/packets", str(self.project), os.path.realpath(self.sandbox / "analysis"))
        self.assertTrue(console_packets.serve(handler))
        ((status, page),) = handler.sent
        self.assertEqual((status, page.count("<section>"), page.count("<form")), (200, 1, 0))
        for want in ("Packets switch: OFF.", "<dd>%s</dd>" % PACKET_ID, "<dt>status</dt><dd>proposed</dd>", "Exact signature payload"):
            self.assertIn(want, page)
        self.assertIn("&lt;sample&gt; &amp;lt;", page)
        finding = {"finding_id": FINDING, "topic": TOPIC, "claim": claim, "fact_ids": FACT_ID, "rule_ids": ""}
        root = os.path.realpath(self.sandbox / "analysis")
        with rehearsal_trust.sandbox_anchor(self.sandbox, self.sandbox / "rehearsal.allowed_signers"), \
                mock.patch.dict(os.environ, HOME=str(self.sandbox)):  # the signed review verifies only against the sandbox anchor
            model = console_findings.load_all(root)
        self.assertEqual(model, {"state": "ok", "broken": 0, "rows": [{
            "agency": SLUG, "finding_id": FINDING, "review_state": "approved", "reason": None, "hold_code": None,
            "f4_verdict": "unavailable", "topic": TOPIC, "claim": claim, "fact_ids": FACT_ID, "rule_ids": "",
            "finding_sha256": finding_review.finding_hash(finding), "payload": finding_review.finding_payload(SLUG, finding),
            "facts": [{"fact_id": FACT_ID, "source_sha256": DIGEST, "page": "2", "quote": PAGES[1]}]}]})

    def test_c1_the_review_is_signed_by_the_sandbox_signer_and_trust_is_restored(self):
        """C1: the packet exists only because a sandbox-signed review of the exact finding verified; approval's globals are restored."""
        signers, check = approval.TRUSTED_SIGNERS, approval._require_root
        self.run_driver()
        self.assertEqual((approval.TRUSTED_SIGNERS, approval._require_root), (signers, check))
        (entry,) = [json.loads(x) for x in (self.sandbox / "analysis/.analysis/finding-reviews.jsonl").read_text().splitlines()]
        self.assertEqual((entry["decision"], entry["agency"], entry["finding_id"], entry["principal"]),
                         ("approve", SLUG, FINDING, "stage20-rehearsal"))
        self.assertIn("BEGIN SSH SIGNATURE", entry["signature"])
        # Outside the sandbox anchor the same review is not trusted: the live trust anchor never saw this key.
        state = finding_review.Reviews(self.sandbox / "analysis").state(SLUG, {
            "finding_id": FINDING, "topic": TOPIC, "claim": CLAIM, "fact_ids": FACT_ID, "rule_ids": ""})
        self.assertNotEqual(state["state"], "approved")

    def test_c1_without_a_review_the_real_lane_holds_the_finding_and_stages_nothing(self):
        """C1 negative: no review -> the real packet path holds FINDING_UNREVIEWED, stages zero packets, and packet_stage fails.

        Catches a driver that fabricates the finding packet (the earlier version imported an unreviewed, held finding as public).
        """
        def seed_only(p):
            return driver._analysis_result(p, json.loads(p.config.read_text(encoding="utf-8")))
        with mock.patch.object(driver, "_finding_review", side_effect=seed_only):
            self.failed("packet_stage", "FINDINGS_PACKET_NOT_STAGED")
        self.assertFalse(self.sandbox.joinpath("analysis/.analysis/packet-outbox").exists())
        self.assertFalse(os.path.lexists(self.project / "public-packets.jsonl"))
        config = json.loads((self.sandbox / "analysis/agencies" / SLUG / "review-config.json").read_text())
        with mock.patch.dict(os.environ, HOME=str(self.sandbox)):
            outcome, _, _, summary, write = analysis_bridge._packets(self.sandbox / "analysis", SLUG, config)
        self.assertEqual((outcome, summary, write), ("PACKET_ALL_HELD", "n=0 held=1 FINDING_UNREVIEWED=1", None))

    def test_c1_export_failure_stops_at_seed(self):
        """C1: an analysis-export that does not export the run fails seed with FINDINGS_EXPORT_FAILED."""
        with mock.patch.object(analysis_export, "main", return_value=75):
            self.failed("seed", "FINDINGS_EXPORT_FAILED")

    def test_c1_dependency_error_is_a_fixed_step_code(self):
        """C1: a dependency error becomes FINDINGS_<STEP>_ERROR and its message is not reported."""
        with mock.patch.object(driver.law, "load_package", side_effect=ValueError("PRIVATE-DETAIL")):
            report = self.failed("seed", "FINDINGS_SEED_ERROR")
        self.assertNotIn("PRIVATE-DETAIL", json.dumps(report))

    def test_c1_an_invalid_export_is_not_received(self):
        """C1: an export the host refuses (EXPORT_INVALID) fails analysis_sync with FINDINGS_RECEIVE_REFUSED."""
        real = analysis_export.main

        def corrupt(argv):
            status = real(argv)
            path = self.sandbox / "export" / RUN / "TEXTS.csv"
            path.write_bytes(path.read_bytes() + b"\n")
            return status
        with mock.patch.object(analysis_export, "main", side_effect=corrupt):
            self.failed("analysis_sync", "FINDINGS_RECEIVE_REFUSED")

    def test_c1_a_pending_pair_fails_analysis_sync(self):
        """C1: a run x agency pair left pending (no matching profile) fails analysis_sync with FINDINGS_SYNC_PENDING."""
        with mock.patch.object(analysis_bridge, "_slug", return_value=None):
            self.failed("analysis_sync", "FINDINGS_SYNC_PENDING")

    def test_c1_a_refused_import_proposes_nothing(self):
        """C1: an outbox packet the importer refuses fails packet_import with FINDINGS_IMPORT_REFUSED."""
        with mock.patch.object(analysis_packets, "_items", return_value=[]):
            self.failed("packet_import", "FINDINGS_IMPORT_REFUSED")
        self.assertFalse(os.path.lexists(self.project / "public-packets.jsonl"))

    def test_c1_an_unavailable_packets_page_fails_console_findings(self):
        """C1: /packets that cannot be rendered fails console_findings with FINDINGS_PACKETS_PAGE."""
        with mock.patch.object(console_packets, "render", side_effect=public_packets.PacketError("journal_invalid")):
            self.failed("console_findings", "FINDINGS_PACKETS_PAGE")

    def test_c1_wrong_packet_payload_fails_console_findings(self):
        """C1: the right row metadata must not qualify anything but one exact visible payload."""
        real = console_packets.serve

        def changed(handler):
            served = real(handler)
            ((status, page),) = handler.sent
            start, end = page.index("<pre>"), page.index("</pre>") + len("</pre>")
            block = page[start:end]
            self.assertEqual(block.count(CLAIM), 1)
            handler.sent[0] = status, page[:start] + mutate(block) + page[end:]
            return served

        for name, mutate in (
            ("changed-claim", lambda block: block.replace(CLAIM, "Different synthetic claim.")),
            ("trailing-space", lambda block: block.replace("</pre>", " </pre>")),
            ("hidden-in-comment", lambda block: "<!--" + block + "-->"),
            ("duplicate", lambda block: block + block),
            # Contract: the payload is compared as VISIBLE review text. Bug caught: a <pre hidden> (or any
            # attribute / hidden wrapper) carrying the exact payload still passing the text comparison.
            ("hidden-pre", lambda block: block.replace("<pre>", "<pre hidden>")),
            ("styled-pre", lambda block: block.replace("<pre>", '<pre style="display:none">')),
            ("aria-hidden-pre", lambda block: block.replace("<pre>", '<pre aria-hidden="true">')),
            ("hidden-wrapper", lambda block: "<div hidden>" + block + "</div>"),
            ("styled-wrapper", lambda block: '<span style="display:none">' + block + "</span>"),
        ):
            with self.subTest(name=name), mock.patch.object(console_packets, "serve", side_effect=changed):
                self.sandbox = self.root / name
                self.project = self.sandbox / "project"
                self.failed("console_findings", "FINDINGS_PACKETS_PAGE")

    def test_c1_an_unreadable_findings_view_fails_console_findings(self):
        """C1: /findings that cannot read the agency fails console_findings with FINDINGS_FINDINGS_PAGE."""
        real = finding_review.list_findings
        calls = []

        def unreadable(*args, **kwargs):
            if not calls:  # the first call is the finding_review step reading the payload to sign
                calls.append(1)
                return real(*args, **kwargs)
            raise finding_review.FindingReviewError("finding_inputs_unavailable")
        with mock.patch.object(finding_review, "list_findings", side_effect=unreadable):
            self.failed("console_findings", "FINDINGS_FINDINGS_PAGE")

    def test_c1_a_console_read_that_writes_fails(self):
        """C1: a readback that changes any sandbox file fails console_findings with FINDINGS_CONSOLE_WROTE."""
        real = console_findings.load_all

        def writes(root):
            (Path(root) / "note").write_text("x")
            return real(root)
        with mock.patch.object(console_findings, "load_all", side_effect=writes):
            self.failed("console_findings", "FINDINGS_CONSOLE_WROTE")


class C1PacketState(Base):
    """Each packet_state refusal, caused by a real change to the project after the import, or by a reader that reports it."""

    def after_import(self, change):
        real = analysis_packets.main

        def wrapped(argv):
            status = real(argv)
            change(str(self.project))
            return status
        return mock.patch.object(analysis_packets, "main", side_effect=wrapped)

    def reader(self, name, change):
        real = getattr(public_packets, name)

        def wrapped(*args, **kwargs):
            return change(real(*args, **kwargs))
        return mock.patch.object(public_packets, name, side_effect=wrapped)

    def test_c1_a_second_packet_fails_the_count(self):
        """C1: two pending packets fail packet_state with FINDINGS_PACKET_COUNT."""
        with self.after_import(lambda project: public_packets.propose(project, dict(PACKET, source="fr-other"))):
            self.failed("packet_state", "FINDINGS_PACKET_COUNT")

    def test_c1_a_withdrawn_packet_fails_the_count(self):
        """C1: no pending packet (the reader lists none, as for an expired one) fails packet_state with FINDINGS_PACKET_COUNT."""
        with self.reader("pending", lambda rows: []):
            self.failed("packet_state", "FINDINGS_PACKET_COUNT")

    def test_c1_a_new_revision_fails_the_identity(self):
        """C1: a re-proposed revision 1 fails packet_state with FINDINGS_PACKET_IDENTITY."""
        with self.after_import(lambda project: public_packets.propose(project, dict(PACKET, text=CLAIM + " Revised."))):
            self.failed("packet_state", "FINDINGS_PACKET_IDENTITY")

    def test_c1_an_approved_packet_is_not_pending(self):
        """C1: an approved packet fails packet_state with FINDINGS_PACKET_NOT_PENDING."""
        with self.reader("pending", lambda rows: [dict(r, status="approved") for r in rows]):
            self.failed("packet_state", "FINDINGS_PACKET_NOT_PENDING")

    def test_c1_a_signed_packet_fails(self):
        """C1: a proposed row that carries a signer fails packet_state with FINDINGS_PACKET_SIGNED."""
        with self.reader("pending", lambda rows: [dict(r, signed_by="synthetic-signer") for r in rows]):
            self.failed("packet_state", "FINDINGS_PACKET_SIGNED")

    def test_c1_a_written_view_fails_as_signed(self):
        """C1: a status view naming a written file fails packet_state with FINDINGS_PACKET_SIGNED."""
        with self.reader("status", lambda doc: dict(doc, packets=[dict(v, file="site-changes/x.json") for v in doc["packets"]])):
            self.failed("packet_state", "FINDINGS_PACKET_SIGNED")

    def test_c1_a_switch_that_is_not_off_fails(self):
        """C1: a publication switch that is not OFF fails packet_state with FINDINGS_SWITCH_NOT_OFF."""
        with self.reader("status", lambda doc: dict(doc, switch="ON")):
            self.failed("packet_state", "FINDINGS_SWITCH_NOT_OFF")

    def test_c1_a_site_change_folder_fails(self):
        """C1: any site-change output fails packet_state with FINDINGS_SITE_CHANGE_WRITTEN."""
        with self.after_import(lambda project: os.mkdir(os.path.join(project, public_packets.OUT), 0o700)):
            self.failed("packet_state", "FINDINGS_SITE_CHANGE_WRITTEN")

    def test_c1_an_extra_journal_event_fails(self):
        """C1: any journal event besides the one proposal fails packet_state with FINDINGS_JOURNAL_UNEXPECTED."""
        with self.after_import(lambda project: public_packets.turn_off(project, "synthetic-operator")):
            self.failed("packet_state", "FINDINGS_JOURNAL_UNEXPECTED")


class C2Conflict(Base):
    def test_c2_a_hash_conflict_is_refused_with_export_conflict(self):
        """C2: the same run re-sent with other bytes is refused as EXPORT_CONFLICT and the retained run is unchanged."""
        with mock.patch.object(analysis_bridge, "receive", wraps=analysis_bridge.receive) as receive:
            report = self.run_driver()
        self.assertEqual(report["codes"], [])
        self.assertEqual(receive.call_count, 2)
        retained = self.sandbox / "analysis/.analysis/runs" / RUN
        self.assertEqual(analysis_bridge._load(retained), analysis_bridge._load(self.sandbox / "export" / RUN))
        self.assertNotEqual(analysis_bridge._load(self.sandbox / "conflict" / RUN), analysis_bridge._load(retained))
        out = StringIO()
        with redirect_stdout(out):
            status = analysis_bridge.sync_main(["--root", str(self.sandbox / "analysis"), "--receive", str(self.sandbox / "conflict")])
        self.assertEqual((status, out.getvalue()), (1, "RECEIVE %s EXPORT_CONFLICT\n" % RUN))
        self.assertEqual(analysis_bridge._load(retained), analysis_bridge._load(self.sandbox / "export" / RUN))

    def test_c2_a_resend_that_is_not_refused_fails_analysis_sync(self):
        """C2: when the re-sent copy is accepted (here identical bytes), analysis_sync fails FINDINGS_CONFLICT_NOT_REFUSED."""
        def identical(p):
            for name, data in analysis_bridge._load(p.export / RUN).items():
                driver._put(p.conflict / RUN / name, data)
        with mock.patch.object(driver, "_conflicting_copy", side_effect=identical):
            self.failed("analysis_sync", "FINDINGS_CONFLICT_NOT_REFUSED")


class C3Outputs(Base):
    def test_c3_outputs_are_hashed_sandbox_files_without_finding_text(self):
        """C3: every output is a sandbox-relative path with its file's sha256; no record or finding text is reported."""
        report = self.run_driver()
        for step in report["steps"]:
            for output in step["outputs"]:
                path = (self.sandbox / output["path"]).resolve()
                self.assertTrue(path.is_relative_to(self.sandbox) and not Path(output["path"]).is_absolute())
                self.assertEqual(output["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertRegex(output["sha256"], r"\A[0-9a-f]{64}\Z")
        text = json.dumps(report)
        for private in PRIVATE_TEXT:
            self.assertNotIn(private, text)

    def test_c3_temporary_files_stay_in_the_sandbox(self):
        """C3: the config check's temporary copy is made inside the sandbox and tempfile's default is restored."""
        before, made, real = tempfile.tempdir, [], tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            made.append(Path(real(*args, **kwargs)))
            return str(made[-1])
        with mock.patch.object(tempfile, "mkdtemp", side_effect=mkdtemp):
            self.assertEqual(self.run_driver()["codes"], [])
        self.assertTrue(made)
        self.assertTrue(all(path.parent == self.sandbox / "tmp" for path in made), made)
        self.assertEqual(tempfile.tempdir, before)
        self.assertEqual(list((self.sandbox / "tmp").iterdir()), [])

    def test_c3_main_prints_the_report_and_exits_zero(self):
        """C3: main runs into an absent sandbox, prints the report as JSON without finding text and exits 0."""
        out = StringIO()
        with redirect_stdout(out):
            status = driver.main(["--sandbox", str(self.sandbox)])
        report = json.loads(out.getvalue())
        self.assertEqual((status, report), (0, self.expected()))
        self.assertEqual(stat.S_IMODE(self.sandbox.stat().st_mode), 0o700)
        for private in PRIVATE_TEXT:
            self.assertNotIn(private, out.getvalue())

    def test_c3_main_exits_one_on_a_failed_step(self):
        """C3: main exits 1 when a step fails."""
        out = StringIO()
        with mock.patch.object(analysis_export, "main", return_value=75), redirect_stdout(out):
            status = driver.main(["--sandbox", str(self.sandbox)])
        self.assertEqual((status, json.loads(out.getvalue())["codes"]), (1, ["FINDINGS_EXPORT_FAILED"]))

    def test_c3_main_refuses_a_used_sandbox(self):
        """C3: main exits 2 for a sandbox that is not empty, and writes nothing there."""
        self.sandbox.mkdir(mode=0o700)
        (self.sandbox / "kept").write_text("kept")
        with redirect_stdout(StringIO()), mock.patch("sys.stderr", new=StringIO()), self.assertRaises(SystemExit) as stop:
            driver.main(["--sandbox", str(self.sandbox)])
        self.assertEqual((stop.exception.code, os.listdir(self.sandbox)), (2, ["kept"]))

    def test_c3_run_refuses_a_shared_sandbox(self):
        """C3: run refuses a group-readable sandbox with ValueError(SANDBOX_INVALID) before writing anything."""
        self.sandbox.mkdir()
        self.sandbox.chmod(0o750)
        with self.assertRaises(ValueError) as refused:
            driver.run(self.sandbox)
        self.assertEqual((refused.exception.args, os.listdir(self.sandbox)), (("SANDBOX_INVALID",), []))

    def test_c3_interface_constants(self):
        """C3: the driver declares stage 16 and a title."""
        self.assertEqual((driver.STAGES, type(driver.TITLE)), (("16",), str))


if __name__ == "__main__":
    unittest.main()
