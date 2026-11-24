"""Stage 11 and 12 evidence from a copied run-outputs folder; fixtures use the real pure writers."""

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.law import load_package
from campaign_tool.records import golive_adapt_gaps as adapter
from campaign_tool.records.outcomes import gap_proposals as gp, received_items as ri, register as reg, timeline as tl

RUN, OTHER = "run_20260101T060000Z_0a1b2c3d", "run_other"
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
FIXTURE = Path("examples/run-report/records-run-report-v1.synthetic.json")
SECRET = "SENTINEL-REQUEST-TEXT"
FILES = {"gaps": adapter.GAPS, "items": adapter.ITEMS, "register": adapter.REGISTER, "timeline": adapter.TIMELINE}
GAP, ITEMS, REG, TL = "GAPS_FILE_", "REGISTER_FILE_ITEMS_", "REGISTER_FILE_REGISTER_", "REGISTER_FILE_TIMELINE_"


def gap_doc(run=RUN, proposals=1, quote="Data is kept 5 years."):
    finding = {"rating": "gap", "rule_ref": {"label": "Civ. Code 1798.90.51(b)"}, "page": 1, "quote": quote,
               "policy_ref": {"document_sha256": "a" * 64, "page": 1, "quote": quote},
               "comparison_ref": {"field": "retention"}, "comparison_reason": "reason"}
    item = {"item_id": f"{1:064x}", "original_sha256": f"{1:064x}", "agency_id": "ag1", "request_id": "r1",
            "kind": "records", "findings": [finding] * proposals}
    received = {"schema": ri.SCHEMA, "run_id": run, "items": [item] * proposals}
    return gp.propose(received, {}, load_package("us-ca"), run_id=run, now=NOW)


def items_doc(run=RUN):
    return ri.collect(sqlite3.connect(":memory:"), Path("."), None, run_id=run)


def register_doc(ref="SYN-1", as_of="2030-01-12"):
    data = {table: [] for table in reg.correspondence.TABLES}
    data["agencies"] = [{"id": "a1", "name": "Synthetic Agency"}]
    data["requests"] = [{"id": "r1", "agency_id": "a1", "external_ref": ref, "submitted_at": "2030-01-10",
                         "received_at": "2030-01-12", "status": "open"}][:ref is not None]
    return reg.build_register(data, as_of=as_of)


def timeline_doc(register=None, run=RUN):
    return tl.build_timeline(register or register_doc(), items_doc(), [], [], [], run_id=run)


def encode(name, doc):
    if isinstance(doc, bytes):
        return doc
    if name == adapter.REGISTER:
        return (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return (ri._canon(doc) + "\n").encode()


class GapsEvidenceTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tmp, self.copies = Path(scratch.name), 0

    def report(self, mode="scheduled", needs=(), stub=False):
        report = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report.update(run_id=RUN, mode=mode)
        if needs:
            next(s for s in report["stages"] if s["name"] == "outputs").update(status="warn", error_code=needs[0])
        report["needs_g"] += [dict(code=c, stage="outputs", severity="action", count=1)
                              for c in (*needs, *["STUB_STAGE_BACKEND"] * stub)]
        self.copies += 1
        path = self.tmp / "reports" / f"{RUN}-{self.copies}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def build(self, name=RUN, **change):
        """Copy of a healthy run folder; a changed file is a document, raw bytes, or None (absent)."""
        docs = {"gaps": gap_doc(), "items": items_doc(), "register": register_doc(), "timeline": timeline_doc()} | change
        self.copies += 1
        root = self.tmp / f"copy{self.copies}" / name
        (root / "outcomes").mkdir(parents=True)
        for directory in (root.parent, root, root / "outcomes"):
            os.chmod(directory, 0o700)
        for key, doc in docs.items():
            if doc is not None:
                path = root / ("" if key == "register" else "outcomes") / FILES[key]
                path.write_bytes(encode(FILES[key], doc))
                os.chmod(path, 0o600)
        return root

    def rows(self, root=None, report=None, now=NOW):
        return adapter.records(root or self.build(), report or self.report(), now)

    def codes(self, copy=None, report=None):
        return [row["codes"] for row in self.rows(self.build(**(copy or {})), report)]

    def cli(self, root, report, *flags):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = adapter.main(["--outputs-dir", str(root), "--run-report", str(report), *flags])
        return status, out.getvalue(), err.getvalue()

    def test_c1_healthy_control_and_cli_exit_codes(self):
        """C1 bugs: no two pass rows (order, kind, actor); a failing or unbound run exiting 0; no stable code."""
        root, report = self.build(), self.report()
        rows = adapter.records(root, report, NOW)
        self.assertEqual([(r["stage"], r["kind"], r["actor"], r["result"], r["codes"]) for r in rows],
                         [("11", "live", "pipeline", "pass", []), ("12", "live", "pipeline", "pass", [])])
        status, out, err = self.cli(root, report, "--json")
        self.assertEqual((status, [r["result"] for r in json.loads(out)], err), (0, ["pass", "pass"], ""))
        status, out, err = self.cli(self.build(gaps=None), report)
        self.assertEqual((status, err, [json.loads(x)["result"] for x in out.splitlines()]), (2, "", ["fail", "pass"]))
        self.assertEqual(self.cli(self.build("other-run"), report), (2, "", "OUTPUTS_RUN_UNBOUND\n"))
        with patch.object(adapter, "_gap_schema", side_effect=OSError):
            self.assertEqual(self.cli(self.build(), report), (2, "", "GAPS_EVIDENCE_BAD_INPUT\n"))

    def test_c2_run_binding_refusals(self):
        """C2 bugs: another run's folder accepted for healthy-looking contents; a group-readable copy or a
        symlink carrying the right name binding; a test, stub, future or malformed report as live proof; healthy-
        looking files counted after the report carries the stage's own writer failure code."""
        loose, bad = self.build(), self.tmp / "bad.json"
        os.chmod(loose, 0o750)
        (self.tmp / "linked").mkdir()
        os.symlink(self.build("alias-target"), self.tmp / "linked" / RUN)
        bad.write_text("{}", encoding="utf-8")
        for root in (self.build("run_20260101T060000Z_ffffffff"), loose, self.tmp / "linked" / RUN):
            with self.subTest(root.name), self.assertRaises(adapter.Unbound):
                self.rows(root)
        for report in (self.report(mode="test"), self.report(stub=True), bad, self.tmp / "absent.json"):
            with self.subTest(report.name), self.assertRaises(adapter.Unbound):
                self.rows(report=report)
        with self.assertRaises(adapter.Unbound):  # finished after the clock
            self.rows(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        # A report carrying the stage's own writer failure code fails only that stage, whatever the files say.
        self.assertEqual(self.codes(report=self.report(needs=("OUTPUTS_GAP_PROPOSALS_UNAVAILABLE",))),
                         [["OUTPUTS_GAP_PROPOSALS_UNAVAILABLE"], []])
        both = self.report(needs=("OUTPUTS_TIMELINE_UNAVAILABLE", "OUTPUTS_REGISTER_UNAVAILABLE"))
        self.assertEqual(self.codes(report=both), [[], ["OUTPUTS_REGISTER_UNAVAILABLE", "OUTPUTS_TIMELINE_UNAVAILABLE"]])

    def test_c3_stage_11_gap_file_must_be_this_runs_and_valid(self):
        """C3 bugs: a gap file of another run passing on folder name; zero proposals passing unbound to the run;
        a legal conclusion or inconsistent counts passing."""
        runless = {k: v for k, v in gap_doc(proposals=0).items() if k != "run_id"}
        cases = [(gap_doc(), []), (gap_doc(OTHER), [GAP + "RUN"]), (gap_doc(proposals=0), []),
                 (gap_doc(OTHER, 0), [GAP + "RUN"]), (runless, [GAP + "SCHEMA"]), ({}, [GAP + "SCHEMA"])]
        for mutate in (lambda d: d.update(legal_conclusion=True), lambda d: d["counts"]["by_confidence"].update(high=9),
                       lambda d: d.update(review_list=["f" * 64])):
            doc = gap_doc()
            mutate(doc)
            cases.append((doc, [GAP + "SCHEMA"]))
        for doc, want in cases:
            self.assertEqual(self.codes({"gaps": doc})[0], want)

    def test_c3_stage_12_files_must_share_the_run_and_the_register_needs_a_request(self):
        """C3 bugs: a register, items or timeline file from another run passing, or two files sharing one
        code; an empty register passing; one missing file failing the other stage."""
        empty, other = register_doc(None), register_doc("OTHER-9", "2031-03-03")
        cases = (({"items": items_doc(OTHER)}, [ITEMS + "RUN"]), ({"timeline": timeline_doc(run=OTHER)}, [TL + "RUN"]),
                 ({"register": other}, [REG + "RUN"]), ({"register": empty, "timeline": timeline_doc(empty)}, [REG + "EMPTY"]),
                 ({"items": items_doc(OTHER), "timeline": timeline_doc(run=OTHER)}, [ITEMS + "RUN", TL + "RUN"]))
        for change, want in cases:
            self.assertEqual(self.codes(change)[1], want)
        self.assertEqual(self.codes({"gaps": None}), [[GAP + "MISSING"], []])
        self.assertEqual(self.codes({"items": None, "register": None, "timeline": None}),
                         [[], [ITEMS + "MISSING", REG + "MISSING", TL + "MISSING"]])

    def test_c3_rows_without_a_request_identity_do_not_bind_the_register(self):
        """C3 bug: a register and timeline that both carry request_count=1 and requests=[{}] (or rows missing an
        agency or request id, or with non-string ids) matched each other and passed stage 12 with no codes."""
        base_reg, base_tl = register_doc(), timeline_doc()
        row = dict(base_reg["requests"][0])
        bad_rows = [{}, {k: v for k, v in row.items() if k != "agency_key"}, {**row, "agency_key": ""},
                    {k: v for k, v in row.items() if k != "request_id"}, {**row, "request_id": 7},
                    {**row, "ledger_request_id": 7}, {**row, "ledger_request_id": ""}]
        for bad in bad_rows:
            copy = {"register": {**base_reg, "requests": [bad]}, "timeline": {**base_tl, "requests": [bad]}}
            self.assertEqual(self.codes(copy)[1], [REG + "SCHEMA", TL + "SCHEMA"], bad)
        # A tracker-only row legitimately has no ledger request id (None); it must still bind.
        tracker_only = {**row, "ledger_request_id": None}
        copy = {"register": {**base_reg, "requests": [tracker_only]}, "timeline": {**base_tl, "requests": [tracker_only]}}
        self.assertEqual(self.codes(copy)[1], [])

    def test_c4_source_is_the_sorted_digest_listing_of_the_file_bytes(self):
        """C4 bugs: a digest ignoring file bytes; an invented digest for files never read."""
        root, report = self.build(), self.report()
        rows = self.rows(root, report)
        sha = {n: hashlib.sha256((root / ("" if n == adapter.REGISTER else "outcomes") / n).read_bytes()).hexdigest()
               for n in FILES.values()}
        listing = "".join(f"{n}\t{sha[n]}\n" for n in sorted(sha) if n != adapter.GAPS)
        self.assertEqual(rows[1]["source"], {"schema": adapter.REGISTER_SCHEMA, "name": RUN,
                                             "sha256": hashlib.sha256(listing.encode()).hexdigest()})
        self.assertEqual(rows[0]["source"]["sha256"],
                         hashlib.sha256(f"{adapter.GAPS}\t{sha[adapter.GAPS]}\n".encode()).hexdigest())
        path = root / "outcomes" / adapter.GAPS
        path.write_bytes(path.read_bytes().replace(b"5 years", b"6 years"))
        self.assertNotEqual(self.rows(root, report)[0]["source"]["sha256"], rows[0]["source"]["sha256"])
        self.assertEqual(self.rows(self.build(gaps=None), report)[0]["source"],
                         {"schema": "records-run-report-v1", "name": report.name,
                          "sha256": hashlib.sha256(report.read_bytes()).hexdigest()})

    def test_c4_rows_carry_codes_only_never_request_text(self):
        """C4 bug: quoted request text, or a parser message, copied into the evidence row."""
        raw = b'{"quote": "' + SECRET.encode() + b'", "quote": 1}'
        for change in ({"gaps": gap_doc(OTHER, quote=SECRET)}, {"gaps": raw}, {"items": raw}, {"timeline": raw}):
            rows = self.rows(self.build(**change))
            self.assertNotIn(SECRET, json.dumps(rows))
            self.assertTrue(all(re.fullmatch(r"(GAPS|REGISTER)_FILE_[A-Z_]+", c) for r in rows for c in r["codes"]))

    def test_c5_strict_bounded_safe_and_read_only(self):
        """C5 bugs: duplicate keys hiding a second run id, NaN or a list passing; a world-readable, hard-linked,
        symlinked or oversize file read; the adapter writing to or touching the copy it inspects."""
        good = encode(adapter.GAPS, gap_doc(proposals=0)).decode()
        dup = good.replace('"run_id":"' + RUN + '"', '"run_id":"' + OTHER + '","run_id":"' + RUN + '"')
        nan = good.replace('"unlinked_items":0', '"unlinked_items":NaN')
        self.assertNotIn(good, (dup, nan))
        for raw in (dup.encode(), nan.encode(), b"[]", b"\xff"):
            self.assertEqual(self.codes({"gaps": raw})[0], [GAP + "INVALID"])
        root = self.build()
        path = root / "outcomes" / adapter.GAPS
        snap = lambda: [(str(p), p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")  # noqa: E731
                        for p in [root, *sorted(root.rglob("*"))]]
        before = snap()
        self.assertEqual(self.rows(root)[0]["codes"], [])
        self.assertEqual(snap(), before)
        with patch.object(adapter, "MAX_BYTES", 100):
            self.assertEqual(self.rows(root)[0]["codes"], [GAP + "UNSAFE"])
        os.link(path, self.tmp / "second-link")
        self.assertEqual(self.rows(root)[0]["codes"], [GAP + "UNSAFE"])  # two links
        os.unlink(self.tmp / "second-link")
        os.chmod(path, 0o644)
        self.assertEqual(self.rows(root)[0]["codes"], [GAP + "UNSAFE"])
        (self.tmp / "elsewhere.json").write_bytes(path.read_bytes())
        os.unlink(path)
        os.symlink(self.tmp / "elsewhere.json", path)
        self.assertEqual(self.rows(root)[0]["codes"], [GAP + "UNSAFE"])
        os.chmod(root / "outcomes", 0o750)
        self.assertEqual(self.rows(root)[1]["codes"], [ITEMS + "UNSAFE", TL + "UNSAFE"])
