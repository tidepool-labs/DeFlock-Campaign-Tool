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
        self.add_agency("agency-a")
        source_hash = "a" * 64
        self.report = {"schema": received_items.SCHEMA, "run_id": self.run_id,
                       "items": [{"original_sha256": source_hash, "agency_id": "agency-a",
                                  "request_id": "request-1", "item_id": "item-1"}]}
        self.approved = {
            "topic": "contradiction", "review_state": "approved", "hold_code": None,
            "finding_id": "finding-1", "finding_sha256": "b" * 64,
            "facts": [{"fact_id": "fact-1", "source_sha256": source_hash, "page": "1", "quote": "Synthetic text."}],
        }
        real_list = findings.fr.list_findings
        self.review_rows = [self.approved]

        def list_findings(root, *, agency):
            return self.review_rows if agency == "agency-a" else real_list(root, agency=agency)

        self.enterContext(patch.object(findings.fr, "list_findings", side_effect=list_findings))

    def add_agency(self, slug):
        config = self.analysis / "agencies" / slug / "review-config.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        profile = self.analysis / "profiles" / f"{slug}.json"
        profile.parent.mkdir(exist_ok=True)
        profile.write_text(json.dumps({"records_agency_id": slug}), encoding="utf-8")
        return config, profile

    def load(self):
        return findings.load_reviewed_contradictions(self.project, self.run_id, self.analysis)

    def write_report(self, report):
        self.report_path.write_text(json.dumps(report), encoding="utf-8")

    def assert_unavailable(self):
        with self.assertRaises(findings.FindingsUnavailable) as caught:
            self.load()  # must raise instead of returning even an already assembled descriptor
        self.assertEqual(str(caught.exception), "followup_findings_unavailable")

    def test_current_report_maps_reviewed_finding(self):
        # Requiring report identity must not stop a legitimate same-run join.
        self.write_report(self.report)
        self.review_rows.extend(self.excluded_rows())
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
                self.assert_unavailable()

    def test_broken_profile_binding_refuses_mixed_agencies(self):
        # A broken binding used to silently drop agency-b and return only the healthy finding.
        self.write_report(self.report)
        self.assertTrue(self.load()[0]["valid"])
        _, profile = self.add_agency("agency-b")
        for case, data in (
            ("missing", None),
            ("malformed", b'{"private-profile-detail":'),
            ("invalid UTF-8", b'\xff'),
            ("not an object", b'[]'),
            ("missing agency ID", b'{}'),
            ("non-string agency ID", b'{"records_agency_id": 2}'),
            ("empty agency ID", b'{"records_agency_id": ""}'),
            ("blank agency ID", b'{"records_agency_id": "   "}'),
            ("duplicate agency ID", b'{"records_agency_id": "agency-a"}'),
        ):
            with self.subTest(case=case):
                if data is None:
                    profile.unlink()
                else:
                    profile.write_bytes(data)
                self.assert_unavailable()

    def test_failed_agency_read_refuses_instead_of_returning_partial_descriptors(self):
        # The real reader raises on malformed config after agency-a has supplied an approved finding.
        self.write_report(self.report)
        self.assertTrue(self.load()[0]["valid"])
        config, _ = self.add_agency("agency-b")
        config.write_text('{"private-review-detail":', encoding="utf-8")
        self.assert_unavailable()

    def excluded_rows(self):
        return [dict(self.approved, finding_id="unreviewed", review_state="unreviewed"),
                dict(self.approved, finding_id="rejected", review_state="rejected"),
                dict(self.approved, finding_id="held", hold_code="synthetic_hold"),
                dict(self.approved, finding_id="unsupported", topic="record-gap")]

    def test_readable_empty_and_excluded_only_sources_still_return_empty(self):
        self.write_report(self.report)
        for rows in ([], self.excluded_rows()):
            with self.subTest(empty=not rows):
                self.review_rows = rows
                self.assertEqual(self.load(), [])

    def test_missing_report_leaves_reviewed_finding_unmapped(self):
        # Absence is not a refusal or grounds to drop the approved finding.
        (descriptor,) = self.load()
        self.assertEqual(descriptor, {
            "kind": "contradiction", "agency_key": "agency-a", "request_id": None,
            "finding_id": "finding-1", "finding_sha256": "b" * 64, "evidence_sha256": None,
            "facts": [], "item_ids": [], "valid": False,
        })
