"""Stage 10-14 source index: receipt -> request/agency -> document -> page or message part, from saved outputs only."""
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "records" / "stage_source_index.py"
_spec = importlib.util.spec_from_file_location("stage_source_index", SCRIPT)
assert _spec is not None and _spec.loader is not None
idx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idx)
wb = idx.wb

MAIN = "c" * 40
SECRET = "Synthetic quote that must never be copied"
H = {name: hashlib.sha256(name.encode()).hexdigest() for name in ("harbor", "valley", "denial", "part", "nopage",
                                                                    "stray", "fee", "unmatched", "followup")}


def _jsonl(*lines):
    return "".join(json.dumps(line) + "\n" for line in lines)


def stage_outputs():
    """Synthetic step outputs per stage, shaped like the real rehearsal outputs (relative path -> text)."""
    return {
        "10": {
            "pair/comparison.json": json.dumps({"run_id": "run_a", "rows": [
                {"agency_id": "ag-harbor", "field": "retention", "evidence": [
                    {"locator": "page 2", "source_id": "sha256:" + H["harbor"]}]},
                {"agency_id": "ag-valley", "field": "retention", "evidence": [
                    {"locator": "page 1", "source_id": "sha256:" + H["valley"]}]},
                {"agency_id": "ag-valley", "field": "deadline", "request_id": "ag-valley-req", "evidence": []}]}),
            "pair/factsheets.json": json.dumps({"run_id": "run_a", "agencies": [
                {"agency_id": "ag-harbor", "fields": {"retention": {"statements": [
                    {"citation": {"document_sha256": H["harbor"], "page": 2}, "quote": SECRET}]}}}]}),
            "pair/inbox/00.eml": "Subject: synthetic\n\n" + SECRET + "\n",
        },
        "11": {
            "replay/gap-proposals.json": json.dumps({"run_id": "run_b", "agencies": [{"agency_id": "ag-valley", "requests": [
                {"request_id": "req-1", "proposals": [{"agency_id": "ag-valley", "item_id": H["denial"],
                                                       "source_id": H["denial"], "page": 1, "quote": SECRET,
                                                       "request_id": "req-1"}]}]}]}),
        },
        "12": {
            "replay/received-items.json": json.dumps({"run_id": "run_c", "items": [
                {"agency_id": "ag-valley", "request_id": "req-1", "item_id": H["part"],
                 "kind_basis": {"page": None, "part": 2, "quote": SECRET}},
                {"agency_id": "ag-valley", "request_id": "req-1", "item_id": H["nopage"],
                 "kind_basis": {"page": None, "quote": None}}]}),
            "replay/timeline.json": json.dumps({"run_id": "run_c", "requests": [
                {"agency_key": "ag-valley", "request_id": "req-1", "events": [{"source": {"item_id": H["part"]}}]}]}),
            "stray/received-items.json": json.dumps({"run_id": "run_s", "items": [
                {"agency_id": None, "request_id": None, "item_id": H["stray"], "kind_basis": {"page": 1}}]}),
        },
        "13": {
            "replay/queue.jsonl": _jsonl(
                {"event": "draft", "snapshot": {"agency_id": "ag-valley", "request_id": "req-2", "item_id": H["fee"],
                                                "source_run_id": "run_d", "body": SECRET,
                                                "evidence": {"item_id": H["fee"],
                                                             "kind_basis": {"page": 3, "quote": SECRET}}}},
                {"event": "draft", "snapshot": {"agency_id": "unmatched", "request_id": "unmatched",
                                                "source_run_id": "run_d", "evidence": {"item_id": H["unmatched"]}}},
                {"event": "draft", "snapshot": {"agency_id": "ag-valley", "request_id": "req-3",
                                                "source_run_id": "run_d", "evidence": {"source_id": "clock:abc"}}}),
        },
        "14": {
            "replay/queue.jsonl": _jsonl(
                {"event": "draft", "snapshot": {"agency_id": "ag-valley", "request_id": "req-3",
                                                "item_id": H["followup"], "source_run_id": "run_e",
                                                "evidence": {"item_id": H["followup"], "kind_basis": {"page": 1}}}}),
        },
    }


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_case(root, outputs=None, main=MAIN, tag="r"):
    """Write receipts and outputs under root and return the agenda path (and the outputs used)."""
    outputs = stage_outputs() if outputs is None else outputs
    root = Path(root)
    rows = []
    for number, stage in enumerate(wb.STAGES):
        base = root / ("stage-" + stage)
        refs = []
        for rel, text in outputs[stage].items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            refs.append({"path": rel, "sha256": sha(text.encode())})
        receipt = base / ("receipt-%s-%s.json" % (tag, stage))
        receipt.write_text(json.dumps({"stage": stage, "tag": tag}), encoding="utf-8")
        rsha = sha(receipt.read_bytes())
        rows.append({"stage": stage, "readiness": wb.READY, "why": [], "next_human_check": "check " + stage,
                     "citation": {"driver_modules": ["rehearse_" + stage], "driver_source_sha256": "d" * 64,
                                  "engine": main, "finished_at": "2026-11-25T12:0%d:00Z" % number,
                                  "receipt": str(receipt), "receipt_sha256_in_set": rsha,
                                  "receipt_sha256_on_disk": rsha,
                                  "steps": [{"step": "write", "code": "OK", "outputs": refs}]}})
    agenda = {"schema": wb.AGENDA_SCHEMA, "main_commit": main, "set_engine": main, "set_status": "QUALIFIED_SET",
              "rows": rows}
    path = root / ("agenda-%s.json" % tag)
    path.write_text(json.dumps(agenda, indent=1), encoding="utf-8")
    return path


def load(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), sha(raw)


def by_doc(index, stage):
    entry = next(s for s in index["stages"] if s["stage"] == stage)
    return {link["document_id"]: link for link in entry["links"]}


class SourceIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, outputs=None):
        return idx.build(*load(write_case(self.root, outputs)))

    def refused(self, code, outputs=None, mutate=None):
        agenda_path = write_case(self.root, outputs)
        if mutate:
            mutate(self.root)
        with self.assertRaises(idx.Refused) as caught:
            idx.build(*load(agenda_path))
        self.assertEqual(str(caught.exception), code)

    def test_links_follow_request_agency_and_locator(self):
        index = self.build()
        self.assertEqual(index["schema"], idx.SCHEMA)
        self.assertIs(index["accepted"], False)
        self.assertEqual([s["stage"] for s in index["stages"]], list(wb.STAGES))
        ten = by_doc(index, "10")
        self.assertEqual(ten[H["harbor"]]["status"], "LINKED")
        self.assertEqual(ten[H["harbor"]]["agencies"], ["ag-harbor"])
        self.assertEqual(ten[H["harbor"]]["requests"], [])
        self.assertEqual(ten[H["harbor"]]["locators"], [{"kind": "page", "value": "2"}])
        self.assertEqual(len(ten[H["harbor"]]["evidence"]), 2)  # comparison and fact sheet agree
        self.assertEqual(ten[H["valley"]]["status"], "LINKED")
        eleven = by_doc(index, "11")[H["denial"]]
        self.assertEqual((eleven["status"], eleven["requests"], eleven["run_id"]), ("LINKED", ["req-1"], "run_b"))
        twelve = by_doc(index, "12")
        self.assertEqual(twelve[H["part"]]["locators"], [{"kind": "part", "value": "2"}])
        self.assertEqual(twelve[H["part"]]["status"], "LINKED")
        self.assertEqual(twelve[H["nopage"]]["status"], "NO_LOCATOR")
        self.assertEqual(twelve[H["stray"]]["status"], "UNATTRIBUTED")
        thirteen = by_doc(index, "13")
        self.assertEqual(thirteen[H["fee"]]["locators"], [{"kind": "page", "value": "3"}])
        self.assertEqual((thirteen[H["fee"]]["requests"], thirteen[H["fee"]]["run_id"]), (["req-2"], "run_d"))
        self.assertEqual(thirteen[H["unmatched"]]["status"], "UNATTRIBUTED")  # "unmatched" placeholder is no request
        self.assertEqual(set(thirteen), {H["fee"], H["unmatched"]})  # a clock id is not a document
        stage10 = next(s for s in index["stages"] if s["stage"] == "10")
        self.assertEqual(stage10["opaque_outputs"], 1)
        self.assertEqual(stage10["counts"]["LINKED"], 2)

    def test_same_document_under_two_requests_or_agencies_is_withheld(self):
        outputs = stage_outputs()
        timeline = json.loads(outputs["12"]["replay/timeline.json"])
        timeline["requests"].append({"agency_key": "ag-valley", "request_id": "req-9",
                                     "events": [{"source": {"item_id": H["part"]}}]})
        outputs["12"]["replay/timeline.json"] = json.dumps(timeline)
        sheets = json.loads(outputs["10"]["pair/factsheets.json"])
        sheets["agencies"][0]["fields"]["retention"]["statements"].append(
            {"citation": {"document_sha256": H["valley"], "page": 1}, "quote": SECRET})
        outputs["10"]["pair/factsheets.json"] = json.dumps(sheets)
        index = self.build(outputs)
        part = by_doc(index, "12")[H["part"]]
        self.assertEqual((part["status"], part["requests"]), ("CROSS_REQUEST", ["req-1", "req-9"]))
        valley = by_doc(index, "10")[H["valley"]]
        self.assertEqual((valley["status"], valley["agencies"]), ("CROSS_AGENCY", ["ag-harbor", "ag-valley"]))
        self.assertEqual(by_doc(index, "10")[H["harbor"]]["status"], "LINKED")

    def test_same_document_in_two_runs_is_two_links(self):
        outputs = stage_outputs()
        outputs["11"]["stray/gap-proposals.json"] = json.dumps({"run_id": "run_b2", "agencies": [
            {"agency_id": "ag-valley", "requests": [{"request_id": "req-7", "proposals": [
                {"item_id": H["denial"], "page": 1}]}]}]})
        stage = next(s for s in self.build(outputs)["stages"] if s["stage"] == "11")
        self.assertEqual(sorted((link["run_id"], link["requests"][0], link["status"]) for link in stage["links"]),
                         [("run_b", "req-1", "LINKED"), ("run_b2", "req-7", "LINKED")])

    def test_explicit_null_request_does_not_inherit_the_outer_request(self):
        outputs = stage_outputs()
        outputs["12"]["replay/nested.json"] = json.dumps({"run_id": "run_c", "request_id": "req-1", "agency_id": "ag-valley",
                                                          "children": [{"request_id": None, "agency_id": None,
                                                                        "item_id": "sha256:" + H["stray"], "page": 4}]})
        link = by_doc(self.build(outputs), "12")[H["stray"]]
        self.assertEqual((link["status"], link["requests"], link["agencies"]), ("UNATTRIBUTED", [], []))

    def test_no_page_is_invented(self):
        outputs = stage_outputs()
        outputs["11"]["replay/gap-proposals.json"] = outputs["11"]["replay/gap-proposals.json"].replace(
            '"page": 1', '"page": 0')
        link = by_doc(self.build(outputs), "11")[H["denial"]]
        self.assertEqual((link["status"], link["locators"]), ("NO_LOCATOR", []))

    def test_changed_missing_or_escaping_evidence_refuses(self):
        self.refused("STAGE_11_EVIDENCE_CHANGED",
                     mutate=lambda root: (root / "stage-11/replay/gap-proposals.json").write_text("{}"))
        self._fresh()
        self.refused("STAGE_12_EVIDENCE_MISSING",
                     mutate=lambda root: (root / "stage-12/replay/timeline.json").unlink())
        self._fresh()
        self.refused("STAGE_13_RECEIPT_CHANGED",
                     mutate=lambda root: (root / "stage-13/receipt-r-13.json").write_text('{"stage": "13"}'))
        self._fresh()
        self.refused("STAGE_14_RECEIPT_MISSING",
                     mutate=lambda root: (root / "stage-14/receipt-r-14.json").unlink())

    def _fresh(self):
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def _agenda_with_path(self, stage, rel):
        agenda_path = write_case(self.root)
        agenda, _ = load(agenda_path)
        row = next(r for r in agenda["rows"] if r["stage"] == stage)
        row["citation"]["steps"][0]["outputs"][0]["path"] = rel
        return agenda

    def test_paths_outside_the_receipt_directory_refuse(self):
        outside = self.root / "outside.json"
        outside.write_text("{}")
        for rel in ("../outside.json", str(outside), "replay/../../outside.json", "./replay/gap-proposals.json"):
            agenda = self._agenda_with_path("11", rel)
            agenda["rows"][1]["citation"]["steps"][0]["outputs"][0]["sha256"] = sha(b"{}")
            with self.assertRaises(idx.Refused) as caught:
                idx.build(agenda, "x")
            self.assertEqual(str(caught.exception), "STAGE_11_EVIDENCE_PATH_OUTSIDE_RECEIPT", rel)

    def test_symlinked_output_or_directory_refuses(self):
        agenda_path = write_case(self.root)
        target = self.root / "stage-11/replay/gap-proposals.json"
        moved = self.root / "elsewhere.json"
        target.rename(moved)
        os.symlink(moved, target)
        with self.assertRaises(idx.Refused) as caught:
            idx.build(*load(agenda_path))
        self.assertEqual(str(caught.exception), "STAGE_11_EVIDENCE_MISSING")
        self._fresh()
        agenda_path = write_case(self.root)
        replay = self.root / "stage-12/replay"
        replay.rename(self.root / "real-replay")
        os.symlink(self.root / "real-replay", replay)
        with self.assertRaises(idx.Refused) as caught:
            idx.build(*load(agenda_path))
        self.assertEqual(str(caught.exception), "STAGE_12_EVIDENCE_PATH_OUTSIDE_RECEIPT")

    def test_agenda_refusals_are_the_workbook_refusals(self):
        agenda, digest = load(write_case(self.root))
        agenda["rows"][2]["citation"]["engine"] = "e" * 40
        with self.assertRaises(idx.Refused) as caught:
            idx.build(agenda, digest)
        self.assertEqual(str(caught.exception), "STAGE_12_RECEIPT_OR_ENGINE_MISMATCH")
        agenda, digest = load(write_case(self.root))
        agenda["rows"][0]["readiness"] = "STALE"
        with self.assertRaises(idx.Refused) as caught:
            idx.build(agenda, digest)
        self.assertEqual(str(caught.exception), "STAGES_NOT_READY_10")

    def test_unparseable_json_output_refuses(self):
        outputs = stage_outputs()
        outputs["13"]["replay/queue.jsonl"] += "not json\n"
        agenda_path = write_case(self.root, outputs)
        with self.assertRaises(idx.Refused) as caught:
            idx.build(*load(agenda_path))
        self.assertEqual(str(caught.exception), "STAGE_13_EVIDENCE_NOT_JSON")

    def test_no_quote_or_body_text_is_copied_and_build_is_deterministic(self):
        agenda_path = write_case(self.root)
        first, second = idx.build(*load(agenda_path)), idx.build(*load(agenda_path))
        self.assertEqual(first, second)
        self.assertNotIn(SECRET, json.dumps(first))
        self.assertNotIn(SECRET, idx.render(first))

    def test_index_binds_each_stage_to_the_workbook_evidence_digest(self):
        agenda, digest = load(write_case(self.root))
        workbook = wb.make(agenda, digest)
        index = idx.build(agenda, digest)
        self.assertEqual([s["evidence_digest"] for s in index["stages"]],
                         [e["evidence_digest"] for e in workbook["entries"]])
        self.assertEqual(index["agenda_sha256"], digest)

    def test_cli_writes_private_files_once(self):
        agenda_path = write_case(self.root)
        out = self.root / "index-out"
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), "build", str(agenda_path), "--out", str(out)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(run.stdout.startswith("INDEX engine=cccccccc 10=linked:2,"), run.stdout)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for name in ("source-index.json", "SOURCE-INDEX.md"):
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o600)
        self.assertEqual(json.loads((out / "source-index.json").read_text()), idx.build(*load(agenda_path)))
        again = subprocess.run([sys.executable, "-B", str(SCRIPT), "build", str(agenda_path), "--out", str(out)],
                               capture_output=True, text=True)
        self.assertEqual((again.returncode, again.stderr.strip()), (2, "REFUSED OUTPUT_DIRECTORY_NOT_NEW"))
        (self.root / "stage-11/replay/gap-proposals.json").write_text("{}")
        changed = subprocess.run([sys.executable, "-B", str(SCRIPT), "build", str(agenda_path), "--out",
                                  str(self.root / "other")], capture_output=True, text=True)
        self.assertEqual((changed.returncode, changed.stderr.strip()), (2, "REFUSED STAGE_11_EVIDENCE_CHANGED"))
        self.assertFalse((self.root / "other").exists())


if __name__ == "__main__":
    unittest.main()
