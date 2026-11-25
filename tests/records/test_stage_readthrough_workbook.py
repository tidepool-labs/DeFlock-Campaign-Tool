"""Stage 10-14 read-through workbook: blank human fields, evidence references, and detection of copied prior-run entries."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "records" / "stage_readthrough_workbook.py"
_spec = importlib.util.spec_from_file_location("stage_readthrough_workbook", SCRIPT)
assert _spec is not None and _spec.loader is not None
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

MAIN = "a" * 40
NEXT = {stage: "check stage " + stage for stage in wb.STAGES}


def agenda(tag="1", main=MAIN):
    rows = []
    for number, stage in enumerate(wb.STAGES):
        rows.append({"stage": stage, "readiness": wb.READY, "why": [], "next_human_check": NEXT[stage],
                     "citation": {"driver_modules": ["rehearse_x" + stage], "driver_source_sha256": "d" * 64,
                                  "engine": main, "finished_at": "2026-11-25T11:5%d:00Z" % number,
                                  "receipt": "/somewhere/%s-receipt.json" % stage,
                                  "receipt_sha256_in_set": tag * 64, "receipt_sha256_on_disk": tag * 64,
                                  "steps": [{"step": "write", "code": "OK",
                                             "outputs": [{"path": "out/%s/%s.json" % (tag, stage), "sha256": stage[-1] * 32 + tag * 32}]}]}})
    return {"schema": wb.AGENDA_SCHEMA, "main_commit": main, "set_engine": main, "set_status": "QUALIFIED_SET", "rows": rows}


def fill(workbook, stage, finding="looked at the rows", **extra):
    entry = next(e for e in workbook["entries"] if e["stage"] == stage)
    entry["human"].update({"reviewer": "R", "checked_on": "2026-11-26", "finding": finding, "outcome": "read"})
    entry["human"].update(extra)


def built(tag="1"):
    return wb.make(agenda(tag), "s" * 64)


class MakeTests(unittest.TestCase):
    def test_five_stage_entries_with_evidence_and_blank_human_fields(self):
        book = built()
        self.assertEqual([e["stage"] for e in book["entries"]], list(wb.STAGES))
        self.assertIs(book["accepted"], False)
        for entry in book["entries"]:
            self.assertEqual(entry["human"], {"reviewer": "", "checked_on": "", "finding": "", "outcome": ""})
            self.assertEqual(entry["engine"], MAIN)
            self.assertEqual(entry["references"][0]["path"], "out/1/%s.json" % entry["stage"])
            self.assertEqual(entry["receipt"], entry["stage"] + "-receipt.json")
            self.assertEqual(entry["next_human_check"], NEXT[entry["stage"]])
        self.assertEqual(len({e["evidence_digest"] for e in book["entries"]}), 5)
        self.assertNotIn("/somewhere", json.dumps(book))
        self.assertIn("Stage 12", wb.render(book))

    def test_refuses_unless_all_five_are_ready_on_the_main_engine(self):
        def broken(edit):
            doc = agenda()
            edit(doc)
            return doc
        cases = {
            "AGENDA_NOT_RECOGNISED": broken(lambda d: d.update(schema="other")),
            "AGENDA_DOES_NOT_COVER_STAGES_10_TO_14": broken(lambda d: d["rows"].pop()),
            "AGENDA_DUPLICATE_STAGE_11": broken(lambda d: d["rows"][2].update(stage="11")),
            "STAGES_NOT_READY_12": broken(lambda d: d["rows"][2].update(readiness="CANDIDATE_ONLY")),
            "AGENDA_ENGINE_NOT_THE_MAIN_COMMIT": broken(lambda d: d.update(set_engine="b" * 40)),
            "STAGE_13_RECEIPT_OR_ENGINE_MISMATCH": broken(lambda d: d["rows"][3]["citation"].update(engine="b" * 40)),
            "STAGE_10_RECEIPT_OR_ENGINE_MISMATCH": broken(lambda d: d["rows"][0]["citation"].update(receipt_sha256_on_disk="9" * 64)),
            "STAGE_14_EVIDENCE_REFERENCES_INCOMPLETE": broken(lambda d: d["rows"][4]["citation"].update(steps=[])),
        }
        for code, doc in cases.items():
            with self.assertRaises(wb.Refused, msg=code) as raised:
                wb.make(doc, "s" * 64)
            self.assertEqual(str(raised.exception), code)


class CheckTests(unittest.TestCase):
    def run_check(self, book, prior=None, current=None):
        return wb.check(book, current or agenda(), prior)

    def test_blank_and_properly_filled_workbooks_have_no_flags(self):
        flags, states = self.run_check(built())
        self.assertEqual((flags, set(states.values())), ([], {"BLANK"}))
        book = built()
        fill(book, "10")
        fill(book, "11", finding="a different finding")
        flags, states = self.run_check(book)
        self.assertEqual(flags, [])
        self.assertEqual(states, {"10": "FILLED", "11": "FILLED", "12": "BLANK", "13": "BLANK", "14": "BLANK"})

    def test_an_entry_copied_from_a_prior_run_is_flagged(self):
        prior = built("2")
        fill(prior, "12", finding="stage 12 was fine")
        book = built()
        book["entries"][2] = copy.deepcopy(prior["entries"][2])  # prior run's whole entry pasted in
        flags, states = self.run_check(book, prior)
        self.assertEqual(flags, [("12", "COPIED_FROM_PRIOR_RUN"), ("12", "EVIDENCE_NOT_CURRENT")])
        self.assertEqual(states["12"], "FLAGGED")

    def test_a_prior_finding_pasted_under_current_evidence_is_flagged(self):
        prior = built("2")
        fill(prior, "10", finding="Rows  match   their SOURCES")
        book = built()
        fill(book, "10", finding="rows match their sources")
        flags, _ = self.run_check(book, prior)
        self.assertEqual(flags, [("10", "COPIED_FROM_PRIOR_RUN")])
        flags, _ = self.run_check(book, None)  # without the prior run it is only a normal filled entry
        self.assertEqual(flags, [])

    def test_prior_findings_on_identical_evidence_are_a_repeat_not_a_copy(self):
        prior = built("1")
        fill(prior, "10", finding="same words")
        book = built("1")
        fill(book, "10", finding="same words")
        self.assertEqual(self.run_check(book, prior)[0], [])

    def test_edited_evidence_fields_and_new_agenda_are_flagged(self):
        book = built()
        book["entries"][1]["references"][0]["sha256"] = "0" * 64
        self.assertEqual(self.run_check(book)[0], [("11", "ENTRY_FIELDS_ALTERED")])  # digest still current, fields not
        self.assertEqual({code for _, code in self.run_check(built("1"), current=agenda("3"))[0]}, {"EVIDENCE_NOT_CURRENT"})

    def test_half_filled_missing_duplicate_unknown_and_shared_findings(self):
        book = built()
        book["entries"][0]["human"]["reviewer"] = "R"
        self.assertEqual(self.run_check(book)[0], [("10", "PARTIAL_HUMAN_ENTRY")])
        book = built()
        book["entries"].pop(4)
        book["entries"].append(copy.deepcopy(book["entries"][0]))
        book["entries"].append({"stage": "99"})
        self.assertEqual(self.run_check(book)[0],
                         sorted([("14", "MISSING_STAGE"), ("10", "DUPLICATE_STAGE"), ("99", "UNKNOWN_STAGE")]))
        book = built()
        fill(book, "13", finding="All good")
        fill(book, "14", finding="all   good")
        self.assertEqual(self.run_check(book)[0], [("13", "SAME_FINDING_UNDER_TWO_STAGES"), ("14", "SAME_FINDING_UNDER_TWO_STAGES")])

    def test_unrecognised_workbooks_and_not_ready_current_agenda_refuse(self):
        with self.assertRaises(wb.Refused):
            wb.check({"schema": "x"}, agenda())
        with self.assertRaises(wb.Refused):
            wb.check(built(), agenda(), {"schema": "x"})
        stale = agenda()
        stale["rows"][0]["readiness"] = "STALE"
        with self.assertRaises(wb.Refused):
            wb.check(built(), stale)


class CommandTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_make_then_check_through_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agenda.json").write_text(json.dumps(agenda()))
            made = self.run_script("make", str(root / "agenda.json"), "--out", str(root / "book"))
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertEqual(sorted(p.name for p in (root / "book").iterdir()), ["WORKBOOK.md", "workbook.json"])
            self.assertEqual(os.stat(root / "book" / "workbook.json").st_mode & 0o777, 0o600)
            again = self.run_script("make", str(root / "agenda.json"), "--out", str(root / "book"))
            self.assertNotEqual(again.returncode, 0)  # an existing output directory is never reused
            before = sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*"))
            ok = self.run_script("check", str(root / "book" / "workbook.json"), str(root / "agenda.json"))
            self.assertEqual((ok.returncode, "flags=0 (no acceptance inferred)" in ok.stdout), (0, True), ok.stdout)
            self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*")), before)  # check writes nothing
            (root / "agenda2.json").write_text(json.dumps(agenda("2")))
            bad = self.run_script("check", str(root / "book" / "workbook.json"), str(root / "agenda2.json"))
            self.assertEqual(bad.returncode, 1)
            self.assertIn("FLAG stage 10 EVIDENCE_NOT_CURRENT", bad.stdout)

    def test_unready_agenda_and_unreadable_input_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = agenda()
            doc["rows"][1]["readiness"] = "UNAVAILABLE"
            (root / "agenda.json").write_text(json.dumps(doc))
            refused = self.run_script("make", str(root / "agenda.json"), "--out", str(root / "book"))
            self.assertEqual((refused.returncode, "STAGES_NOT_READY_11" in refused.stderr), (2, True))
            self.assertFalse((root / "book").exists())
            missing = self.run_script("make", str(root / "nope.json"), "--out", str(root / "book2"))
            self.assertEqual(missing.returncode, 2)


if __name__ == "__main__":
    unittest.main()
