"""The finding's review and the received-items report establish different bindings."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import followup_findings as findings
from campaign_tool.records.outcomes import received_items


class ReceivedItemsBindingTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = root / "project"
        self.analysis = root / "analysis"
        self.run_id = "current-run"
        outcomes = self.project / "run-outputs" / self.run_id / "outcomes"
        outcomes.mkdir(parents=True)
        self.report_path = outcomes / "received-items.json"
        config = self.analysis / "agencies" / "agency-a" / "review-config.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        profiles = self.analysis / "profiles"
        profiles.mkdir()
        (profiles / "agency-a.json").write_text('{"records_agency_id": "agency-a"}', encoding="utf-8")
        source_hash = "a" * 64
        self.report = {"schema": received_items.SCHEMA, "run_id": self.run_id,
                       "items": [{"original_sha256": source_hash, "agency_id": "agency-a",
                                  "request_id": "request-1", "item_id": "item-1"}]}
        self.enterContext(patch.object(findings.fr, "list_findings", return_value=[{
            "topic": "contradiction", "review_state": "approved", "hold_code": None,
            "finding_id": "finding-1", "finding_sha256": "b" * 64,
            "facts": [{"fact_id": "fact-1", "source_sha256": source_hash, "page": "1", "quote": "Synthetic text."}],
        }]))

    def load(self):
        return findings.load_reviewed_contradictions(self.project, self.run_id, self.analysis)

    def write_report(self, report):
        self.report_path.write_text(json.dumps(report), encoding="utf-8")

    def test_current_report_maps_reviewed_finding(self):
        # Requiring report identity must not stop a legitimate same-run join.
        self.write_report(self.report)
        (descriptor,) = self.load()
        self.assertTrue(descriptor["valid"])
        self.assertEqual((descriptor["finding_id"], descriptor["request_id"], descriptor["item_ids"]),
                         ("finding-1", "request-1", ["item-1"]))

    def test_present_report_requires_identity_and_item_list(self):
        # The stale-run and wrong-schema cases keep the same joinable items as the current report.
        for case, report in (
            ("stale run", dict(self.report, run_id="previous-run")),
            ("wrong schema", dict(self.report, schema="unrelated-v1")),
            ("missing run", {k: v for k, v in self.report.items() if k != "run_id"}),
            ("missing schema", {k: v for k, v in self.report.items() if k != "schema"}),
            ("missing items", {k: v for k, v in self.report.items() if k != "items"}),
            ("non-list items", dict(self.report, items={"item-1": self.report["items"][0]})),
        ):
            with self.subTest(case=case):
                self.write_report(report)
                with self.assertRaises(findings.FindingsUnavailable) as caught:
                    self.load()
                self.assertEqual(str(caught.exception), "followup_findings_unavailable")

    def test_missing_report_leaves_reviewed_finding_unmapped(self):
        # Absence is not a refusal or grounds to drop the approved finding.
        (descriptor,) = self.load()
        self.assertEqual(descriptor, {
            "kind": "contradiction", "agency_key": "agency-a", "request_id": None,
            "finding_id": "finding-1", "finding_sha256": "b" * 64, "evidence_sha256": None,
            "facts": [], "item_ids": [], "valid": False,
        })
