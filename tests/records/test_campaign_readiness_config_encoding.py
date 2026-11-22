"""Campaign readiness inventories the byte encodings accepted by campaign_config."""
import json
from unittest import mock

from campaign_tool.records import campaign_readiness
from tests.records import test_campaign_readiness as readiness_tests


class CampaignConfigEncodingTests(readiness_tests.Base):
    def setUp(self):
        super().setUp()
        self.project.mkdir(mode=0o700)
        readiness_tests.write(self.project / ".campaign-root", str(self.campaign) + "\n")
        self.agencies = self.campaign / "content" / "agencies.json"
        readiness_tests.write(self.agencies, "[]")
        self.config = self.project / "campaign.json"
        self.config_text = json.dumps(
            {"schema_version": 1, "campaign_id": "offline-test", "display_name": "Café"},
            ensure_ascii=False,
        )

    def test_supported_encodings_report_present_fields(self):
        # Decoding as UTF-8 text before parsing used to lose the projection on BOM input.
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
                         "utf-32", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding):
                self.config.write_bytes(self.config_text.encode(encoding))
                report, status = self.report()
                self.assertEqual(status, 0)
                self.assertEqual(report["campaign_fields"]["campaign_id"], "present")
                self.assertEqual(report["campaign_fields"]["display_name"], "present")
                self.assertEqual(report["campaign_fields"]["timezone"], "missing")
                self.assertNotIn("READINESS_CONFIG_INVALID", self.codes(report))

    def test_malformed_json_and_invalid_bytes_are_invalid(self):
        # A bad supplied config must not silently fall back to missing/defaults.
        for raw in (b'{"schema_version":', self.config_text.encode("utf-8").replace(b"\xc3\xa9", b"\xff")):
            with self.subTest(raw=raw):
                self.config.write_bytes(raw)
                report, status = self.report()
                self.assertEqual(status, 1)
                self.assertEqual(report["campaign_fields"]["campaign_id"], "invalid")
                self.assertIn("READINESS_CONFIG_INVALID", self.codes(report))
                self.assertNotIn("READINESS_CONFIG_MISSING", self.codes(report))

    def test_absent_config_is_missing_not_invalid(self):
        report, status = self.report()
        self.assertEqual(status, 0)
        self.assertEqual(report["campaign_fields"]["campaign_id"], "missing")
        self.assertIn("READINESS_CONFIG_MISSING", self.codes(report))
        self.assertNotIn("READINESS_CONFIG_INVALID", self.codes(report))

    def test_bom_does_not_relax_agencies_parser(self):
        # Using the byte parser for all readiness JSON would accept this input.
        self.agencies.write_bytes("[]".encode("utf-8-sig"))
        done = self.run_cli("--json")
        self.assertEqual((done.returncode, done.stdout, done.stderr.strip()),
                         (2, "", "READINESS_AGENCIES_INVALID"))

    def test_unavailable_projection_is_invalid_even_when_validation_succeeds(self):
        # The two reads can disagree; validation alone cannot supply field presence.
        self.config.write_bytes(self.config_text.encode("utf-8"))
        read = campaign_readiness._read

        def read_without_projection(path, limit):
            if path == str(self.config):
                raise campaign_readiness._Bad
            return read(path, limit)

        with mock.patch.object(campaign_readiness, "_read", side_effect=read_without_projection):
            report = campaign_readiness.collect(self.project)
        self.assertEqual(report["campaign_fields"]["campaign_id"], "invalid")
        self.assertIn("READINESS_CONFIG_INVALID", self.codes(report))
