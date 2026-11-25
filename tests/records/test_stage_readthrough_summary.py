"""Stage 10-14 read-through summary: a recorded human finding only on the stage's own linked sources, else unreviewed."""
import copy
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
SCRIPT = HERE.parents[2] / "scripts" / "records" / "stage_readthrough_summary.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


summ = _load("stage_readthrough_summary", SCRIPT)
case = _load("stage_source_index_case", HERE.with_name("test_stage_source_index.py"))
idx, wb, H = summ.index_mod, summ.wb, case.H


D10H, D10V, D11, D12 = H["harbor"][:12], H["valley"][:16], H["denial"][:12], H["part"][:12]
OK_PROSE = "Proposal quotes the stored denial as written."


def fill(workbook, stage, finding, cited, **extra):
    entry = next(e for e in workbook["entries"] if e["stage"] == stage)
    entry["human"].update({"reviewer": "R", "checked_on": "2026-11-27", "finding": finding, "cited": cited,
                           "outcome": "read"})
    entry["human"].update(extra)
    return workbook


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.agenda_path = case.write_case(self.root)
        self.agenda, self.agenda_sha = case.load(self.agenda_path)
        self.workbook = wb.make(self.agenda, self.agenda_sha)
        self.index = idx.build(self.agenda, self.agenda_sha)

    def tearDown(self):
        self._tmp.cleanup()

    def run_summary(self, workbook=None, index=None, agenda=None, agenda_sha=None, prior=None):
        return summ.summarize(self.workbook if workbook is None else workbook,
                              self.agenda if agenda is None else agenda,
                              self.agenda_sha if agenda_sha is None else agenda_sha,
                              self.index if index is None else index, prior)

    def statuses(self, summary):
        return {s["stage"]: (s["status"], tuple(s["flags"])) for s in summary["stages"]}

    def stage_of(self, stage, finding, cited):
        summary = self.run_summary(workbook=fill(copy.deepcopy(self.workbook), stage, finding, cited))
        return summary, self.statuses(summary)[stage]

    def refused(self, code, **kwargs):
        with self.assertRaises(summ.Refused) as caught:
            self.run_summary(**kwargs)
        self.assertEqual(str(caught.exception), code)

    def test_blank_workbook_is_unreviewed_never_accepted(self):
        summary = self.run_summary()
        self.assertEqual(summary["schema"], "stage-readthrough-summary-v2")
        self.assertIs(summary["accepted"], False)
        self.assertEqual({s["status"] for s in summary["stages"]}, {"UNREVIEWED"})
        self.assertTrue(all(s["human"] is None for s in summary["stages"]))
        twelve = next(s for s in summary["stages"] if s["stage"] == "12")
        self.assertEqual([link["document_id"] for link in twelve["linked_sources"]], [H["part"]])
        self.assertEqual(sorted(link["status"] for link in twelve["unreviewed_sources"]), ["NO_LOCATOR", "UNATTRIBUTED"])

    def test_finding_with_structured_citations_on_the_stage_own_sources_is_recorded_verbatim(self):
        cited = "document %s page 1; request req-1" % D11
        summary, state = self.stage_of("11", OK_PROSE, cited)
        self.assertEqual(state, ("FINDING_RECORDED", ()))
        self.assertEqual({self.statuses(summary)[s][0] for s in ("10", "12", "13", "14")}, {"UNREVIEWED"})
        eleven = next(s for s in summary["stages"] if s["stage"] == "11")
        self.assertEqual((eleven["human"]["finding"], eleven["human"]["cited"]), (OK_PROSE, cited))
        self.assertIn("Cited (each entry checked against the linked sources): " + cited, summ.render(summary))
        self.assertIs(summary["accepted"], False)
        for stage, cited in (("12", "document %s part 2;request REQ-1" % D12.upper()),
                             ("12", "  Document   %s  Part 2 ;  " % D12),
                             ("10", "document %s page 2; document %s page 1" % (D10H, D10V)),
                             ("10", "document %s" % D10V)):
            self.assertEqual(self.stage_of(stage, "Rows match their stored sources.", cited)[1],
                             ("FINDING_RECORDED", ()), cited)

    def test_wrong_workbook_header_alone_is_refused(self):
        wrong = copy.deepcopy(self.workbook)
        wrong["main_commit"] = "f" * 40  # entries unchanged: the merged workbook check alone does not flag this
        self.assertEqual(wb.check(wrong, self.agenda)[0], [])
        self.refused("WORKBOOK_HEADER_NOT_CURRENT", workbook=wrong)
        claims = copy.deepcopy(self.workbook)
        claims["accepted"] = True
        self.refused("WORKBOOK_HEADER_NOT_CURRENT", workbook=claims)

    def test_swapped_agenda_or_index_is_refused(self):
        other_root = self.root / "other"
        other_root.mkdir()
        other_agenda, other_sha = case.load(case.write_case(other_root, tag="o"))
        self.refused("WORKBOOK_AGENDA_MISMATCH", agenda=other_agenda, agenda_sha=other_sha)
        other_index = idx.build(other_agenda, other_sha)
        self.refused("INDEX_AGENDA_MISMATCH", index=other_index)
        engine = copy.deepcopy(self.index)
        engine["main_commit"] = "e" * 40
        self.refused("INDEX_ENGINE_MISMATCH", index=engine)
        self.refused("INDEX_NOT_RECOGNISED", index={"schema": "other"})

    def test_edited_index_is_not_reproducible(self):
        edited = copy.deepcopy(self.index)
        twelve = next(s for s in edited["stages"] if s["stage"] == "12")
        next(link for link in twelve["links"] if link["document_id"] == H["nopage"])["status"] = "LINKED"
        self.refused("INDEX_NOT_REPRODUCIBLE", index=edited)
        swapped = copy.deepcopy(self.index)
        swapped["stages"][0]["links"], swapped["stages"][1]["links"] = \
            swapped["stages"][1]["links"], swapped["stages"][0]["links"]
        self.refused("INDEX_NOT_REPRODUCIBLE", index=swapped)

    def test_evidence_changed_after_the_index_is_refused(self):
        (self.root / "stage-13/replay/queue.jsonl").write_text("{}\n")
        self.refused("STAGE_13_EVIDENCE_CHANGED")

    def test_citations_outside_the_stage_own_sources_are_withheld(self):
        cases = {
            # RETURN t_03dc7263 item 1: the whole part token is compared, never a prefix of it
            ("12", "document %s part 2.1" % D12): ("FINDING_CITES_MISSING_PART",),
            ("12", "document %s part 2-evil" % D12): ("FINDING_CITES_MISSING_PART",),
            ("12", "document %s part 2." % D12): ("FINDING_CITES_MISSING_PART",),
            ("12", "document %s part \uff12" % D12): ("FINDING_CITES_MISSING_PART",),
            ("12", "document %s part 3" % D12): ("FINDING_CITES_MISSING_PART",),
            # RETURN t_03dc7263 item 2: a request unknown to the run is withheld, as is a known other one
            ("11", "document %s page 1; request req-unknown" % D11): ("FINDING_CITES_UNKNOWN_REQUEST",),
            ("11", "document %s page 1; request req-2" % D11): ("FINDING_CITES_OTHER_REQUEST",),
            ("10", "document %s page 2; request req-1" % D10H): ("FINDING_CITES_OTHER_REQUEST",),
            ("11", "request req-1"): ("FINDING_WITHOUT_CITATION",),
            ("12", "document %s" % H["stray"][:16]): ("FINDING_CITES_UNLINKED_DOCUMENT",),
            ("12", "document %s" % H["nopage"][:12]): ("FINDING_CITES_UNLINKED_DOCUMENT",),
            ("13", "document %s page 1" % D11): ("FINDING_CITES_UNLINKED_DOCUMENT",),
            ("11", "document %s page 1; document %s" % (D11, "0123456789ab" * 2)): ("FINDING_CITES_UNLINKED_DOCUMENT",),
            ("11", "document %s page 5" % D11): ("FINDING_CITES_MISSING_PAGE",),
            ("12", "document %s page 1" % D12): ("FINDING_CITES_MISSING_PAGE",),
            ("10", "document %s page 1" % D10H): ("FINDING_CITES_MISSING_PAGE",),
            # anything not in a listed form is never guessed at
            ("11", "document %s page 1, request req-1" % D11): ("FINDING_CITATION_UNPARSED", "FINDING_WITHOUT_CITATION"),
            ("11", "page 1 of document %s" % D11): ("FINDING_CITATION_UNPARSED", "FINDING_WITHOUT_CITATION"),
            ("11", "document %s page 0" % D11): ("FINDING_CITATION_UNPARSED", "FINDING_WITHOUT_CITATION"),
            ("11", "document %s page 1." % D11): ("FINDING_CITATION_UNPARSED", "FINDING_WITHOUT_CITATION"),
            ("11", "document %s page 1; request req-1 req-2" % D11): ("FINDING_CITATION_UNPARSED",),
            ("11", "document %s" % D11[:11]): ("FINDING_CITATION_UNPARSED", "FINDING_WITHOUT_CITATION"),
        }
        for (stage, cited), codes in cases.items():
            summary, state = self.stage_of(stage, OK_PROSE, cited)
            self.assertEqual(state, ("WITHHELD", codes), cited)
            self.assertIsNone(next(s for s in summary["stages"] if s["stage"] == stage)["human"])

    def test_prose_carrying_its_own_reference_is_withheld(self):
        cited = "document %s page 1; request req-1" % D11
        for text in ("See req-unknown.", "Matches req-2 as well.", "Message part 2.1 is fine.",
                     "Message part 2-evil fits.", "The denial is on page 5.", "Item %s fits." % D11,
                     "Checked on 2026-11-27.", "Fine, see the follow-up.", "Fine \u2013 nothing else.",
                     "\u0405ee the denial.", "Same as req-1.", "Quoted at p 1.", "Value 12345 differs."):
            self.assertEqual(self.stage_of("11", text, cited)[1], ("WITHHELD", ("FINDING_PROSE_HAS_CITATION",)), text)
        for text in ("Proposal quotes the denial; it's stored as written (no change)!", "Matches - nothing else.",
                     "Read 2 rows in 2026.", '"Exactly" as stored: yes.'):
            self.assertEqual(self.stage_of("11", text, cited)[1], ("FINDING_RECORDED", ()), text)

    def test_a_prefix_fitting_two_linked_documents_is_ambiguous(self):
        links = [{"status": "LINKED", "requests": [], "document_id": "ab" * 6 + tail * 52,
                  "locators": [{"kind": "page", "value": "1"}]} for tail in ("0", "1")]
        self.assertEqual(summ._finding_flags("Fine.", "document %s page 1" % ("ab" * 6), links, links),
                         ["FINDING_CITATION_AMBIGUOUS"])
        self.assertEqual(summ._finding_flags("Fine.", "document %s page 1" % ("ab" * 6 + "1"), links, links), [])

    def test_cross_request_source_never_supports_a_finding(self):
        outputs = case.stage_outputs()
        outputs["14"]["replay/queue.jsonl"] = case._jsonl(
            {"snapshot": {"agency_id": "ag-valley", "request_id": "req-3", "item_id": H["followup"],
                          "source_run_id": "run_e", "evidence": {"item_id": H["followup"], "kind_basis": {"page": 1}}}},
            {"snapshot": {"agency_id": "ag-valley", "request_id": "req-4", "item_id": H["followup"],
                          "source_run_id": "run_e"}})
        root = self.root / "cross"
        root.mkdir()
        agenda, digest = case.load(case.write_case(root, outputs))
        workbook = fill(wb.make(agenda, digest), "14", "Follow up cites the reply.",
                        "document %s page 1; request req-3" % H["followup"][:12])
        summary = summ.summarize(workbook, agenda, digest, idx.build(agenda, digest))
        fourteen = next(s for s in summary["stages"] if s["stage"] == "14")
        self.assertEqual((fourteen["status"], fourteen["flags"]), ("WITHHELD", ["FINDING_WITHOUT_LINKED_SOURCE"]))
        self.assertEqual([link["status"] for link in fourteen["unreviewed_sources"]], ["CROSS_REQUEST"])

    def test_swapped_stage_entries_cannot_pass(self):
        workbook = fill(fill(copy.deepcopy(self.workbook), "10", "Harbor matches.", "document %s page 2" % D10H),
                        "11", OK_PROSE, "document %s page 1" % D11)
        ten, eleven = workbook["entries"][0], workbook["entries"][1]
        ten["stage"], eleven["stage"] = "11", "10"
        state = self.statuses(self.run_summary(workbook=workbook))
        self.assertEqual(state["10"][0], "WITHHELD")
        self.assertEqual(state["11"][0], "WITHHELD")
        self.assertIn("EVIDENCE_NOT_CURRENT", state["10"][1])

    def test_workbook_flags_withhold(self):
        partial = copy.deepcopy(self.workbook)
        partial["entries"][2]["human"]["finding"] = "half done"
        self.assertEqual(self.statuses(self.run_summary(workbook=partial))["12"], ("WITHHELD", ("PARTIAL_HUMAN_ENTRY",)))
        no_cited = fill(copy.deepcopy(self.workbook), "11", OK_PROSE, "")
        self.assertEqual(self.statuses(self.run_summary(workbook=no_cited))["11"], ("WITHHELD", ("PARTIAL_HUMAN_ENTRY",)))
        fee = "document %s page 3; request req-2" % H["fee"][:12]
        prior = fill(copy.deepcopy(self.workbook), "13", "Fee reply matches.", fee)
        prior["entries"][3]["evidence_digest"] = "0" * 64
        current = fill(copy.deepcopy(self.workbook), "13", "Fee reply matches.", fee)
        self.assertEqual(self.statuses(self.run_summary(workbook=current))["13"], ("FINDING_RECORDED", ()))
        state = self.statuses(self.run_summary(workbook=current, prior=prior))
        self.assertEqual(state["13"], ("WITHHELD", ("COPIED_FROM_PRIOR_RUN",)))

    def test_cli_exit_codes_and_private_output(self):
        def write(name, value):
            path = self.root / name
            path.write_text(json.dumps(value))
            return str(path)

        index_path = write("index.json", self.index)
        blank = write("blank.json", self.workbook)
        out = self.root / "summary-out"
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), blank, str(self.agenda_path), index_path, "--out",
                              str(out)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip().splitlines()[-1],
                         "SUMMARY 10=UNREVIEWED 11=UNREVIEWED 12=UNREVIEWED 13=UNREVIEWED 14=UNREVIEWED withheld=0 "
                         "(no acceptance inferred)")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for name in ("summary.json", "SUMMARY.md"):
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o600)
        self.assertIs(json.loads((out / "summary.json").read_text())["accepted"], False)
        self.assertNotIn(case.QUOTE_CANARY, (out / "SUMMARY.md").read_text())
        withheld = write("withheld.json", fill(copy.deepcopy(self.workbook), "11", OK_PROSE,
                                               "document %s page 1; request req-2" % D11))
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), withheld, str(self.agenda_path), index_path],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertIn("FLAG stage 11 FINDING_CITES_OTHER_REQUEST", run.stdout)
        wrong = copy.deepcopy(self.workbook)
        wrong["main_commit"] = "f" * 40
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), write("wrong.json", wrong), str(self.agenda_path),
                              index_path], capture_output=True, text=True)
        self.assertEqual((run.returncode, run.stderr.strip(), run.stdout), (2, "REFUSED WORKBOOK_HEADER_NOT_CURRENT", ""))


if __name__ == "__main__":
    unittest.main()
