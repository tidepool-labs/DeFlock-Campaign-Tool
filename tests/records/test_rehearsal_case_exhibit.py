"""Stage case exhibit reader: open a case, follow a difference to its locators and timeline, withhold on any break."""
import copy
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import rehearsal_case_exhibit as exhibit

SHA_POLICY = "sha256:" + "a" * 64
SHA_ORIGINAL = "b" * 64
AGENCY, OTHER, FIELD = "ca-valley-pd", "ca-harbor-pd", "sharing_limits"


def comparison():
    def row(agency, field, status):
        return {"agency_id": agency, "field": field, "status": status, "reason": "documentary_criterion_discrepancy",
                "citation": {"label": "Civ. Code 1798.90.55(b)"}, "expected": "restrictions documented",
                "observed": {"restrictions_documented": False},
                "evidence": [{"locator": "page 1", "source_id": SHA_POLICY}]}
    return {"schema": exhibit.COMPARISON_SCHEMA, "legal_conclusion": False, "review_required": True, "run_id": "run-10",
            "rows": [row(AGENCY, FIELD, "gap"), row(AGENCY, "retention", "meets"), row(OTHER, FIELD, "gap")]}


def timeline():
    return {"schema": exhibit.TIMELINE_SCHEMA, "review_required": True, "run_id": "run-12",
            "requests": [{"request_id": "req-1", "agency_key": AGENCY, "stage": "acknowledged", "issues": [],
                          "events": [{"date": "2026-11-25", "type": "acknowledged",
                                      "source": {"item_id": "item-1", "kind": "received_item"}}]}]}


def received():
    return {"legal_conclusion": False, "review_required": True,
            "items": [{"item_id": "item-1", "request_id": "req-1", "agency_id": AGENCY, "kind": "response",
                       "original_sha256": SHA_ORIGINAL, "diagnostics": [],
                       "kind_basis": {"page": 1, "quote": "We have received your request."}}]}


def case(**change):
    docs = {"c": comparison(), "t": timeline(), "r": received()}
    for key, edit in change.items():
        edit(docs[key])
    return exhibit.open_case(docs["c"], docs["t"], docs["r"], AGENCY, FIELD)


class OpenCaseTests(unittest.TestCase):
    def test_follows_difference_to_both_locators_and_the_timeline(self):
        result = case()
        self.assertEqual(result["status"], exhibit.OPEN)
        self.assertEqual(result["stage_10_difference"]["policy_locators"], [{"locator": "page 1", "source_id": SHA_POLICY}])
        located = result["stage_12_case"]["received_locators"]
        self.assertEqual([(x["item_id"], x["locator"], x["original_sha256"]) for x in located],
                         [("item-1", "page 1", SHA_ORIGINAL)])
        self.assertEqual(result["stage_12_case"]["timeline_events"],
                         [{"date": "2026-11-25", "type": "acknowledged", "item_id": "item-1"}])
        self.assertIs(result["legal_conclusion"], False)
        self.assertIn("Policy source: page 1 @ " + SHA_POLICY, exhibit.render(result))

    def test_a_mail_part_citation_is_a_locator_and_never_gets_a_page(self):
        def mail(docs):
            docs["items"][0]["kind_basis"] = {"page": None, "part": 2, "quote": "mail quote"}
        result = case(r=mail)
        self.assertEqual(result["stage_12_case"]["received_locators"][0]["locator"], "message part 2")

    def test_changed_agency_withholds_everywhere(self):
        def rename_row(docs):
            for r in docs["rows"]:
                if r["agency_id"] == AGENCY:
                    r["agency_id"] = "ca-unrelated-pd"
        self.assertEqual(case(c=rename_row)["reasons"], ["NO_COMPARISON_ROW"])
        self.assertEqual(case(t=lambda d: d["requests"][0].update(agency_key="ca-unrelated-pd"))["reasons"],
                         ["NO_REQUEST_FOR_AGENCY"])
        self.assertEqual(case(r=lambda d: d["items"][0].update(agency_id=OTHER))["reasons"],
                         ["RECEIVED_ITEM_AGENCY_MISMATCH"])

    def test_removed_locators_withhold_and_leak_no_content(self):
        def drop_policy(docs):
            docs["rows"][0]["evidence"][0].pop("locator")
        def drop_source(docs):
            docs["rows"][0]["evidence"][0]["source_id"] = "sha256:short"
        def drop_quote(docs):
            docs["items"][0]["kind_basis"].pop("quote")
        def drop_hash(docs):
            docs["items"][0]["original_sha256"] = ""
        def unavailable(docs):
            docs["items"][0]["diagnostics"] = ["page_locator_unavailable"]
        for key, edit, code in (("c", drop_policy, "POLICY_LOCATOR_MISSING"), ("c", drop_source, "POLICY_LOCATOR_MISSING"),
                                ("r", drop_quote, "RECEIVED_LOCATOR_MISSING"), ("r", drop_hash, "RECEIVED_LOCATOR_MISSING"),
                                ("r", unavailable, "RECEIVED_LOCATOR_MISSING")):
            result = case(**{key: edit})
            self.assertEqual((result["status"], result["reasons"]), (exhibit.WITHHELD, [code]), edit.__name__)
            self.assertNotIn("stage_10_difference", result)
            self.assertNotIn(SHA_POLICY, json.dumps(result))
            self.assertIn("no comparison content", exhibit.render(result))

    def test_item_not_in_timeline_and_foreign_request_withhold(self):
        self.assertEqual(case(t=lambda d: d["requests"][0]["events"][0]["source"].update(item_id="other"))["reasons"],
                         ["NO_TIMELINE_EVENT_FOR_ITEM"])
        self.assertEqual(case(r=lambda d: d["items"][0].update(request_id="req-2"))["reasons"], ["RECEIVED_ITEM_MISSING"])

    def test_only_a_gap_row_opens_and_a_duplicate_row_is_ambiguous(self):
        self.assertEqual(case(c=lambda d: d["rows"][0].update(status="meets"))["reasons"], ["NOT_A_DIFFERENCE"])
        self.assertEqual(case(c=lambda d: d["rows"].append(copy.deepcopy(d["rows"][0])))["reasons"],
                         ["AMBIGUOUS_COMPARISON_ROW"])

    def test_unrecognised_or_legal_conclusion_input_is_withheld(self):
        self.assertEqual(case(c=lambda d: d.update(schema="other"))["reasons"], ["INPUT_NOT_RECOGNISED"])
        self.assertEqual(case(t=lambda d: d.update(legal_conclusion=True))["reasons"], ["INPUT_NOT_RECOGNISED"])
        self.assertEqual(case(r=lambda d: d.update(review_required=False))["reasons"], ["INPUT_NOT_RECOGNISED"])
        self.assertEqual(case(c=lambda d: d.pop("legal_conclusion"))["status"], exhibit.OPEN)  # absent: nothing claimed
        self.assertEqual(exhibit.open_case(None, None, None, AGENCY, FIELD)["reasons"], ["INPUT_NOT_RECOGNISED"])

    def test_inputs_are_not_modified(self):
        docs = comparison(), timeline(), received()
        before = copy.deepcopy(docs)
        exhibit.open_case(*docs, AGENCY, FIELD)
        self.assertEqual(docs, before)


class CommandTests(unittest.TestCase):
    def run_main(self, root, agency, mutate=None):
        docs = {"comparison": comparison(), "timeline": timeline(), "received": received()}
        if mutate:
            mutate(docs)
        args = []
        for name, doc in docs.items():
            path = root / (name + ".json")
            path.write_text(json.dumps(doc))
            args += ["--" + name, str(path)]
        out = root / "out"
        with redirect_stdout(StringIO()), patch.object(socket, "socket", side_effect=AssertionError("network")):
            code = exhibit.main(args + ["--agency", agency, "--field", FIELD, "--out", str(out)])
        return code, out

    def test_writes_only_two_private_files_and_signals_the_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, out = self.run_main(root, AGENCY)
            self.assertEqual(code, 0)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["EXHIBIT.md", "exhibit.json"])
            self.assertEqual(os.stat(out / "exhibit.json").st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((out / "exhibit.json").read_text())["status"], exhibit.OPEN)

    def test_wrong_agency_exits_nonzero_and_unreadable_input_is_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_main(Path(tmp), "ca-unrelated-pd")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads((out / "exhibit.json").read_text())["reasons"],
                             ["NO_REQUEST_FOR_AGENCY", "NO_COMPARISON_ROW"])
        with tempfile.TemporaryDirectory() as tmp:
            args = ["--comparison", tmp + "/missing", "--timeline", tmp + "/missing", "--received", tmp + "/missing",
                    "--agency", AGENCY, "--field", FIELD, "--out", tmp + "/out"]
            with redirect_stdout(StringIO()):
                self.assertEqual(exhibit.main(args), 1)
            self.assertEqual(json.loads(Path(tmp, "out", "exhibit.json").read_text())["reasons"], ["INPUT_NOT_RECOGNISED"])

    def test_refuses_to_reuse_an_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_main(Path(tmp), AGENCY)
            with self.assertRaises(FileExistsError):
                self.run_main(Path(tmp), AGENCY)


if __name__ == "__main__":
    unittest.main()
