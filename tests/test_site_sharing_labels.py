"""DF1125-09 (slice 2): the agency page's pre-meeting and vendor-portal sharing sections follow labels.json.

English builds keep the exact text the pages always had (asserted against literal sections captured from main before
the change); a labels.json that overrides every new key leaves none of the English defaults in those sections, and the
Spanish draft covers every key. Offline, synthetic copies of examples/fictional-campaign, example.org / the portal host
only.
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
PRE_KEYS = ("agency.pre_meeting_heading", "agency.pre_meeting_found", "agency.pre_meeting_record_link",
            "agency.pre_meeting_not_found", "agency.pre_meeting_rule")
SHARING_KEYS = ("agency.sharing_heading", "agency.sharing_intro", "agency.sharing_portal_link",
                "agency.sharing_count_one", "agency.sharing_count_many", "agency.sharing_col_state",
                "agency.sharing_col_kind", "agency.sharing_col_agencies", "agency.sharing_kind_local",
                "agency.sharing_kind_state", "agency.sharing_kind_federal", "agency.sharing_kind_private",
                "agency.sharing_kind_unknown", "agency.sharing_outside", "agency.sharing_more", "agency.sharing_rule",
                "agency.sharing_bulletin")
KEYS = PRE_KEYS + SHARING_KEYS
SHARED = ("agency.law_package", "agency.law_status_draft")  # slice-1 keys these sections now also use
FOUND = {"status": "found", "checked_on": "2026-10-31", "meeting_on": "2026-10-20", "body": "Cedar City Council",
         "source_url": "https://example.org/cedar/minutes-2026-10-20.pdf?a=1&b=2"}
NOT_FOUND = {"status": "not_found_yet", "checked_on": "2026-10-31"}
RECIPIENTS = [("Cedar County Sheriff", "CA", "local"), ("California Highway Patrol", "CA", "state"),
              ("Reno Test PD", "NV", "local"), ("Example Federal Task Force", "CA", "federal"),
              ("Example Data Co", "OR", "private"), ("Unlisted Office", "WA", "unknown")]
# Captured from main 674cc3c7 (before slice 2) with finish tooling; the English build must keep these bytes exactly.
PRE_FOUND = (
    '<section class="pre-meeting"><h4>Public meeting before the camera program</h4><p>Cedar City Council discussed '
    'it on 2026-10-20 (<a href="https://example.org/cedar/minutes-2026-10-20.pdf?a=1&amp;b=2" rel="noopener norefer'
    'rer">official record</a>). Checked 2026-10-31.</p><p>Rule: Civ. Code § 1798.90.55(a). Law package: draft, not '
    'legally reviewed.</p></section>'
)
PRE_NOT_FOUND = (
    '<section class="pre-meeting"><h4>Public meeting before the camera program</h4><p>We have not found a record of'
    ' one yet (checked 2026-10-31).</p><p>Rule: Civ. Code § 1798.90.55(a). Law package: draft, not legally reviewed'
    '.</p></section>'
)
SHARING_SECTION = (
    '<section class="agency-sharing"><h4>Who the vendor portal lists as receiving data</h4><p>As listed on <a href='
    '"https://transparency.flocksafety.com/cedar-ca-pd" rel="noopener noreferrer">the vendor transparency portal</a'
    '>, transcribed by the organizer (checked 2026-10-30): 6 agencies.</p><table><thead><tr><th scope="col">State</'
    'th><th scope="col">Agencies</th></tr></thead><tbody><tr><td>CA</td><td>3</td></tr><tr><td>NV</td><td>1</td></t'
    'r><tr><td>OR</td><td>1</td></tr><tr><td>WA</td><td>1</td></tr></tbody></table><table><thead><tr><th scope="col'
    '">Kind</th><th scope="col">Agencies</th></tr></thead><tbody><tr><td>local</td><td>2</td></tr><tr><td>state</td'
    '><td>1</td></tr><tr><td>federal</td><td>1</td></tr><tr><td>private</td><td>1</td></tr><tr><td>unknown</td><td>'
    '1</td></tr></tbody></table><p>Out of state and federal:</p><ul><li>Example Federal Task Force, CA (federal)</l'
    'i><li>Reno Test PD, NV (local)</li><li>Example Data Co, OR (private)</li><li>Unlisted Office, WA (unknown)</li'
    '></ul><p>Sharing rule: Civ. Code § 1798.90.55(b).</p><p>Attorney General bulletin: Cal. Dept. of Justice Infor'
    'mation Bulletin 2023-DLE-06 (Oct. 27, 2023), interpreting Civ. Code § 1798.90.55(b).</p><p>Law package: draft,'
    ' not legally reviewed.</p></section>'
)
SHARED_OVERRIDES = {"agency.law_package": "SLAW0: {status}", "agency.law_status_draft": "SLAW1"}


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


def write_fixture(root, meeting=FOUND, recipients=RECIPIENTS):
    """Give ca-cedar-police a pre-implementation meeting and a transcribed portal recipient list."""
    path = root / "content" / "agencies.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    profile = next(a for a in document if a["agency_id"] == "ca-cedar-police").setdefault("profile", {})
    profile["pre_implementation_meeting"] = meeting
    profile["sharing"] = {"checked_on": "2026-10-30", "source_url": "https://transparency.flocksafety.com/cedar-ca-pd",
                          "recipients": [{"name": n, "state": s, "kind": k} for n, s, k in recipients]}
    path.write_text(json.dumps(document), encoding="utf-8")


class PreMeetingSharingLabelsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sharing-labels-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

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
        write_fixture(self.root)
        self.build()
        for name in ("agencies/cedar.html", "agencies.html"):
            with self.subTest(page=name):
                page = self.read(name)
                self.assertEqual(section(page, "pre-meeting"), PRE_FOUND)
                self.assertEqual(section(page, "agency-sharing"), SHARING_SECTION)
        write_fixture(self.root, meeting=NOT_FOUND, recipients=RECIPIENTS[:1])
        self.build()
        page = self.read("agencies/cedar.html")
        self.assertEqual(section(page, "pre-meeting"), PRE_NOT_FOUND)
        self.assertIn("(checked 2026-10-30): 1 agency.</p>", section(page, "agency-sharing"))  # the singular count

    def test_every_key_overridden_leaves_no_english_default(self):
        many = RECIPIENTS + [(f"Nevada Office {i}", "NV", "local") for i in range(100)]
        cases = ((FOUND, many, ("agency.pre_meeting_not_found", "agency.sharing_count_one")),
                 (NOT_FOUND, RECIPIENTS[:1], ("agency.pre_meeting_found", "agency.pre_meeting_record_link",
                                               "agency.sharing_count_many", "agency.sharing_more",
                                               "agency.sharing_outside", "agency.sharing_kind_state",
                                               "agency.sharing_kind_federal", "agency.sharing_kind_private",
                                               "agency.sharing_kind_unknown")))
        for meeting, recipients, unused in cases:
            write_fixture(self.root, meeting=meeting, recipients=recipients)
            self.build({key: override(key) for key in KEYS} | SHARED_OVERRIDES)
            for name in ("agencies/cedar.html", "agencies.html"):
                page = self.read(name)
                html = section(page, "pre-meeting") + section(page, "agency-sharing")
                self.assertIn('<section class="pre-meeting">', html, name)
                self.assertIn('<section class="agency-sharing">', html, name)
                text = html.replace('<section class="pre-meeting">', "").replace('<section class="agency-sharing">', "")
                for key in KEYS:
                    with self.subTest(meeting=meeting["status"], page=name, key=key):
                        if key not in unused:
                            self.assertIn(f"OVR{KEYS.index(key)}:", html)
                        for piece in fragments(key):
                            self.assertNotIn(piece, text)  # (the class attribute names no label)
                with self.subTest(meeting=meeting["status"], page=name, key="law package"):
                    self.assertIn("<p>SLAW0: SLAW1</p></section>", html)  # rule lines use the slice-1 law-package label
                    self.assertIn("SLAW0: SLAW1</p></section><section", html)  # the pre-meeting rule line too
                    self.assertNotIn("Law package:", html)
                    self.assertNotIn(DEFAULTS["agency.law_status_draft"], html)

    def test_more_than_one_hundred_outside_keeps_the_english_tail(self):
        many = RECIPIENTS + [(f"Nevada Office {i}", "NV", "local") for i in range(100)]
        write_fixture(self.root, recipients=many)
        self.build()
        html = section(self.read("agencies/cedar.html"), "agency-sharing")
        self.assertIn("</ul><p>and 4 more</p>", html)  # 104 outside rows, 100 listed
        self.assertIn("(checked 2026-10-30): 106 agencies.</p>", html)

    def test_spanish_draft_builds(self):
        write_fixture(self.root)
        self.build({key: ES[key] for key in KEYS + SHARED})
        for name in ("agencies/cedar.html", "agencies.html"):
            with self.subTest(page=name):
                page = self.read(name)
                pre, sharing = section(page, "pre-meeting"), section(page, "agency-sharing")
                self.assertIn("Cedar City Council lo trató el", pre)
                self.assertIn("Paquete legal: borrador, sin revisión legal.</p>", pre)
                self.assertIn("6 agencias.</p>", sharing)
                self.assertIn("Example Federal Task Force, CA (federal)</li>", sharing)
                self.assertIn("Example Data Co, OR (privada)</li>", sharing)
                self.assertIn("Paquete legal: borrador, sin revisión legal.</p>", sharing)
                for english in ("Public meeting before", "official record", "Who the vendor portal", "Agencies</th>",
                                "Out of state and federal", "Sharing rule", "Law package"):
                    self.assertNotIn(english, pre + sharing)

    def test_listed_kind_uses_the_kind_label(self):
        write_fixture(self.root)
        self.build({key: override(key) for key in KEYS})
        sharing = section(self.read("agencies/cedar.html"), "agency-sharing")
        self.assertIn(f"Reno Test PD, NV (OVR{KEYS.index('agency.sharing_kind_local')}:)</li>", sharing)
        self.assertIn(f"<tr><td>OVR{KEYS.index('agency.sharing_kind_federal')}:</td><td>1</td></tr>", sharing)


if __name__ == "__main__":
    unittest.main()
