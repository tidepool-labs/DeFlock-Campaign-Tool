"""Offline packet acceptance; no network, credentials or live promotion."""
import copy
import hashlib
import json
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from campaign_tool.records import console_page_golive, golive_ledger
from campaign_tool.records import golive_phone_check_packet as packet


def ledger_rows(live):
    rows = golive_ledger.build([], [], datetime(2026, 11, 24, tzinfo=timezone.utc))
    for row in rows:
        row["state"] = "LIVE" if row["stage"] in live else "BUILT"
    return rows


def saved_page(live):
    sent = []
    handler = SimpleNamespace(path="/golive", options={"run_report_path": "/synthetic/run-report.json"},
                              _send=lambda status, body: sent.append(body.encode()))
    with mock.patch.object(console_page_golive, "_reports", return_value=[]), \
            mock.patch.object(console_page_golive.golive_evidence, "load", return_value=[]), \
            mock.patch.object(golive_ledger, "build", return_value=ledger_rows(live)):
        console_page_golive.serve(handler)
    return sent[0]


class PhoneCheckPacketTests(unittest.TestCase):
    def setUp(self):
        self.binding = {"origin": "https://admin.deflockslo.com", "seal_sha256": "a" * 64,
                        "console_head": "b" * 40}
        rows = ledger_rows({"2", "A2", "A3"})
        rows[0]["label"] = "PRIVATE_LEDGER_LABEL"
        self.cli = json.dumps({"rows": rows, "summary": "2 of 22 pipeline stages live",
                               "live_count": 2}).encode()
        self.page = saved_page({"2", "A2", "A3"})
        self.capture = {"binding": dict(self.binding), "scope": "hosted_protected",
                        "page_sha256": hashlib.sha256(self.page).hexdigest(),
                        "cli_sha256": hashlib.sha256(self.cli).hexdigest()}
        self.denial = {"binding": dict(self.binding), "scope": "hosted_protected",
                       "signed_out": {"/": "ACCESS_LOGIN", "/golive": "DENIED"},
                       "unauthorized": {"/": "DENIED", "/golive": "DENIED"}}

    def build(self):
        return packet.build(expected=self.binding, page_bytes=self.page, cli_bytes=self.cli,
                            capture=self.capture, denial=self.denial)

    def replace_page(self, content):
        self.page = content
        self.capture["page_sha256"] = hashlib.sha256(content).hexdigest()

    def test_matching_saved_artifacts_prepare_one_sanitized_phone_check(self):
        # The packet includes console live IDs but the dashboard count is pipeline only.
        result = self.build()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["url"], "https://admin.deflockslo.com/golive")
        self.assertEqual(result["live_stage_ids"], ["2", "A2", "A3"])
        self.assertEqual((result["pipeline_live"], result["pipeline_total"]), (2, 22))
        self.assertIn("MATCH", result["recommendation"])
        self.assertNotIn("PRIVATE_LEDGER_LABEL", json.dumps(result))

    def test_same_count_different_stage_ids_holds(self):
        # Matching totals must not mask a live-stage swap on the console.
        self.replace_page(saved_page({"3", "A2", "A3"}))
        self.assertEqual(self.build(), {"status": "HOLD", "reason": "PARITY_MISMATCH"})

    def test_missing_partial_or_non_production_denial_holds(self):
        # A loopback drill or only one route/actor must not clear the hosted access gate.
        full = copy.deepcopy(self.denial)
        for denial in (None, dict(full, scope="sandbox"), dict(full, signed_out={"/": "DENIED"}),
                       dict(full, unauthorized={"/": "DENIED", "/golive": "ACCESS_LOGIN"}),
                       dict(full, signed_out={"/": "DENIED", "/golive": "UNREACHABLE"})):
            with self.subTest(denial=denial):
                with mock.patch.object(self, "denial", denial):
                    self.assertEqual(self.build(), {"status": "HOLD", "reason": "DENIAL_UNVERIFIED"})

    def test_past_seal_or_other_deployment_cannot_be_reused(self):
        for receipt, field, value, reason in (
            (self.capture, "seal_sha256", "c" * 64, "CAPTURE_UNVERIFIED"),
            (self.denial, "console_head", "d" * 40, "DENIAL_UNVERIFIED"),
            (self.denial, "origin", "https://elsewhere.invalid", "DENIAL_UNVERIFIED"),
        ):
            with self.subTest(field=field), mock.patch.dict(receipt["binding"], {field: value}):
                self.assertEqual(self.build(), {"status": "HOLD", "reason": reason})

    def test_changed_artifact_is_not_the_bound_capture(self):
        # Both artifacts still parse and agree after ignorable whitespace, but are not the captured bytes.
        for field in ("page", "cli"):
            with self.subTest(field=field), mock.patch.object(self, field, getattr(self, field) + b"\n"):
                self.assertEqual(self.build(), {"status": "HOLD", "reason": "CAPTURE_UNVERIFIED"})

    def test_missing_reader_cannot_clear_gate(self):
        with mock.patch.dict(sys.modules, {"campaign_tool.records.golive_parity": None}):
            self.assertEqual(self.build(), {"status": "HOLD", "reason": "PARITY_READER_UNAVAILABLE"})

    def test_invalid_bound_page_returns_only_fixed_reason(self):
        self.replace_page(b"PRIVATE_INVALID_PAGE_CONTENT")
        self.assertEqual(self.build(), {"status": "HOLD", "reason": "PARITY_UNVERIFIED"})
