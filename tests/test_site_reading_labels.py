"""DF1125-09 (slice 1): the agency page's policy-check and contract-check readings follow labels.json.

English builds keep the exact text the pages always had (asserted against literal sections captured from main before
the change); a labels.json that overrides every new key leaves none of the English defaults in those sections, and the
Spanish draft covers every key. Offline, synthetic copies of examples/fictional-campaign, example.org URLs only.
"""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.labels import ALLOWED, DEFAULTS, PLACEHOLDER
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
ES = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
TODAY = date(2026, 11, 1)
POLICY_KEYS = ("agency.policy_check_heading", "agency.policy_check_addressed", "agency.policy_check_unclear",
               "agency.policy_check_not_found", "agency.policy_check_checked", "agency.policy_check_link")
CONTRACT_KEYS = ("agency.contract_check_heading", "agency.contract_check_addressed", "agency.contract_check_unclear",
                 "agency.contract_check_not_found", "agency.contract_check_scope", "agency.contract_check_checked",
                 "agency.contract_check_link", "agency.contract_check_checklist")
SHARED_KEYS = ("agency.check_page", "agency.law_package", "agency.law_package_unavailable")
KEYS = POLICY_KEYS + CONTRACT_KEYS + SHARED_KEYS
POLICY_URL, CONTRACT_URL = "https://example.org/cedar/alpr-policy.pdf", "https://example.org/cedar/contract.pdf?id=7&v=2"
POLICY_IDS = ("authorized_purposes", "authorized_users_training", "security_monitoring", "sharing_restrictions",
              "custodian_title", "accuracy_correction", "retention_destruction")
CONTRACT_IDS = ("term_renewal", "price_changes", "data_ownership", "retention", "sharing_network", "vendor_data_use",
                "audit_search_logs", "termination_data_exit")
# Captured from main 4a391c3f (before DF1125-09) with finish tooling; the English build must keep these bytes exactly.
POLICY_SECTION = (
    '<section class="policy-check"><h4>What the posted policy covers (organizer reading, not a legal revi'
    'ew)</h4><ul><li>Authorized purposes for using the system and collecting information: addressed (page'
    ' 3)</li><li>Job titles of the people authorized to access or use the system, or to collect ALPR info'
    'rmation, and training requirements: not found in the posted policy</li><li>Monitoring of security an'
    'd of privacy-law requirements: not found in the posted policy</li><li>Purposes, processes and restri'
    'ctions for selling, sharing or transferring information: not found in the posted policy</li><li>Titl'
    'e of the custodian or owner responsible for the system: not found in the posted policy</li><li>Measu'
    'res for accuracy and error correction: unclear</li><li>How long information is kept and how it is de'
    'cided whether and when to destroy it: not found in the posted policy</li></ul><p>Checked 2026-10-31 '
    'against <a href="https://example.org/cedar/alpr-policy.pdf" rel="noopener noreferrer">the posted pol'
    'icy</a>. Law package: draft, not legally reviewed.</p></section>'
)
CONTRACT_SECTION = (
    '<section class="contract-check"><h4>What the contract says (organizer reading, not a legal review)</'
    'h4><ul><li>Term and automatic renewal: addressed (page 1) <span class="contract-check-note">— Renews'
    ' yearly</span></li><li>Price changes: not found in the linked contract</li><li>Data ownership: uncle'
    'ar (page 12)</li><li>Retention: not found in the linked contract</li><li>Sharing and national-networ'
    'k access: not found in the linked contract</li><li>Vendor use of data: not found in the linked contr'
    'act</li><li>Audit and search-log access: not found in the linked contract</li><li>Termination and da'
    'ta return or deletion: not found in the linked contract</li></ul><p>Not found describes this reading'
    ', not whether a term exists elsewhere.</p><p>Checked 2026-10-31 against <a href="https://example.org'
    '/cedar/contract.pdf?id=7&amp;v=2" rel="noopener noreferrer">the linked contract</a>. Checklist: draf'
    't.</p></section>'
)
CONTRACT_SECTION_H3 = (
    '<section class="contract-check"><h3>What the contract says (organizer reading, not a legal review)</'
    'h3><ul><li>Term and automatic renewal: addressed (page 1) <span class="contract-check-note">— Renews'
    ' yearly</span></li><li>Price changes: not found in the linked contract</li><li>Data ownership: uncle'
    'ar (page 12)</li><li>Retention: not found in the linked contract</li><li>Sharing and national-networ'
    'k access: not found in the linked contract</li><li>Vendor use of data: not found in the linked contr'
    'act</li><li>Audit and search-log access: not found in the linked contract</li><li>Termination and da'
    'ta return or deletion: not found in the linked contract</li></ul><p>Not found describes this reading'
    ', not whether a term exists elsewhere.</p><p>Checked 2026-10-31 against <a href="https://example.org'
    '/cedar/contract.pdf?id=7&amp;v=2" rel="noopener noreferrer">the linked contract</a>. Checklist: draf'
    't.</p></section>'
)


def section(page, cls):
    """The first ``<section class="cls">`` of ``page`` through its ``</section>``; "" when absent."""
    start = page.find(f'<section class="{cls}">')
    return "" if start < 0 else page[start:page.index("</section>", start) + len("</section>")]


def override(key):
    """Override text for ``key``: a marker plus every placeholder its default allows (so values still render)."""
    return f"OVR{KEYS.index(key)}:" + "".join(" {" + name + "}" for name in sorted(ALLOWED[key]))


def fragments(key):
    """The literal English pieces of ``key``'s default (between placeholders) long enough to be distinctive."""
    return [piece.strip() for piece in PLACEHOLDER.split(DEFAULTS[key])[::2] if len(piece.strip()) >= 4]


class ReadingLabelsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-reading-labels-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def write_fixture(self):
        path = self.root / "content" / "agencies.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        profile = next(a for a in document if a["agency_id"] == "ca-cedar-police")["profile"]
        profile["alpr_policy"] = {"url": POLICY_URL, "source_url": "https://example.org/cedar/policies",
                                  "checked_on": "2026-10-30"}
        policy: dict[str, dict] = {i: {"status": "not_found"} for i in POLICY_IDS}
        policy.update(authorized_purposes={"status": "addressed", "page": 3}, accuracy_correction={"status": "unclear"})
        profile["policy_check"] = {"checked_on": "2026-10-31", "policy_url": POLICY_URL, "elements": policy}
        profile["contract"] = {"vendor": "Example Vendor Co (fictional)", "start": "2024-07-01", "end": "2027-06-30",
                               "renewal_note": "", "source_url": CONTRACT_URL, "checked_on": "2026-10-30"}
        contract: dict[str, dict] = {i: {"status": "not_found"} for i in CONTRACT_IDS}
        contract.update(term_renewal={"status": "addressed", "page": 1, "note": "Renews yearly"},
                        data_ownership={"status": "unclear", "page": 12})
        profile["contract_check"] = {"checked_on": "2026-10-31", "contract_url": CONTRACT_URL, "elements": contract}
        path.write_text(json.dumps(document), encoding="utf-8")

    def build(self, labels=None):
        if labels is not None:
            (self.root / "content" / "labels.json").write_text(
                json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        build_site(self.root, today=TODAY)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def test_new_keys_are_defaults_with_spanish_drafts(self):
        for key in KEYS:
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS)
                self.assertIn(key, ES, "examples/labels/es.json has no draft")
                self.assertEqual(set(PLACEHOLDER.findall(ES[key])), ALLOWED[key])

    def test_english_build_unchanged(self):
        self.write_fixture()
        self.build()
        cedar, agencies = self.read("agencies/cedar.html"), self.read("agencies.html")
        self.assertEqual(section(cedar, "policy-check"), POLICY_SECTION)
        self.assertEqual(section(agencies, "policy-check"), POLICY_SECTION)
        self.assertEqual(section(cedar, "contract-check"), CONTRACT_SECTION_H3)  # the agency page's own h3 section
        self.assertEqual(section(agencies, "contract-check"), CONTRACT_SECTION)  # the agencies card (h4)

    def test_law_package_unavailable_is_a_label(self):
        self.write_fixture()
        with mock.patch("campaign_tool.site.law", load_package=mock.Mock(side_effect=ValueError("synthetic"))):
            self.build()
        self.assertIn("Law package: status unavailable.</p>", section(self.read("agencies/cedar.html"), "policy-check"))
        with mock.patch("campaign_tool.site.law", load_package=mock.Mock(side_effect=ValueError("synthetic"))):
            self.build({key: override(key) for key in SHARED_KEYS})
        policy = section(self.read("agencies/cedar.html"), "policy-check")
        self.assertIn(f"OVR{KEYS.index('agency.law_package')}: OVR{KEYS.index('agency.law_package_unavailable')}:", policy)
        self.assertNotIn("status unavailable", policy)

    def test_every_key_overridden_leaves_no_english_default(self):
        self.write_fixture()
        self.build({key: override(key) for key in KEYS})
        pages = (("agencies/cedar.html", ("policy-check", "contract-check")),
                 ("agencies.html", ("policy-check", "contract-check")))
        for name, classes in pages:
            page = self.read(name)
            for cls in classes:
                html = section(page, cls)
                self.assertTrue(html, f"{name}: no {cls} section")
                keys = (POLICY_KEYS if cls == "policy-check" else CONTRACT_KEYS) + SHARED_KEYS[:1]
                for key in keys:
                    with self.subTest(page=name, section=cls, key=key):
                        self.assertIn(f"OVR{KEYS.index(key)}:", html)
                        for piece in fragments(key):
                            self.assertNotIn(piece, html)
        self.assertIn(f"OVR{KEYS.index('agency.law_package')}: ", section(self.read("agencies/cedar.html"), "policy-check"))

    def test_spanish_draft_builds(self):
        self.write_fixture()
        self.build({key: ES[key] for key in KEYS})
        policy = section(self.read("agencies/cedar.html"), "policy-check")
        self.assertIn(ES["agency.policy_check_heading"], policy)
        self.assertNotIn(DEFAULTS["agency.policy_check_heading"], policy)
        self.assertIn(ES["agency.contract_check_scope"], section(self.read("agencies/cedar.html"), "contract-check"))


if __name__ == "__main__":
    unittest.main()
