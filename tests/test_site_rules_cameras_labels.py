"""DF1125-09 (slice 3): the agency page's local-rules block, the cameras-list counters and the empty suspected-locations
line follow labels.json.

English builds keep the exact text the pages always had (asserted against literal output captured from main 26e2d09f
before the change); a labels.json that overrides every new key leaves none of the English defaults in those places, and
the Spanish draft covers every key. Offline, synthetic copies of examples/fictional-campaign only.
"""
import json
import re
import unittest

from campaign_tool.labels import ALLOWED, DEFAULTS, PLACEHOLDER
from campaign_tool.site import build_site
from tests import test_content_suspected as suspected_case  # module imports: their TestCases are not re-collected here
from tests import test_site_cameras_page as cameras_page
from tests import test_site_local_rules as local_rules
from tests.test_site_sharing_labels import ES

OTHER, rule, point, NODE = local_rules.OTHER, local_rules.rule, cameras_page.point, cameras_page.NODE

LOCAL_KEYS = ("agency.local_rules_heading", "agency.local_rules_notice", "agency.local_rule_in_force",
              "agency.local_rule_future", "agency.local_rule_past", "agency.local_rule_label", "agency.local_rule_id",
              "agency.local_rule_duty", "agency.local_rule_effective", "agency.local_rule_open",
              "agency.local_rule_source", "agency.local_rule_checked")
CAMERA_KEYS = ("cameras.group_count_one", "cameras.group_count_many", "cameras.group_more")
SUSPECTED_KEYS = ("suspected.none",)
KEYS = LOCAL_KEYS + CAMERA_KEYS + SUSPECTED_KEYS
LIGHTING = "local-us-ca-cedar-lighting"


def three_rules():
    """In force (open end), past (closed end) and not yet in force, all on ca-cedar-police, build date 2021-04-01."""
    return [rule(), rule(rule_id=OTHER, citation="Cedar Sign Code 9", duty="Post a sign.", effective_to="2020-12-31"),
            rule(rule_id=LIGHTING, citation="Cedar Light Code 2", duty="Log the lights.", effective_from="2022-01-01")]


def _article(citation, state, rule_id, duty, start, end):
    return (f'<article class="local-rule"><h4>{citation}</h4><p><span class="status-pill">{state}</span> <span '
            'class="status-pill">local rule, organizer-transcribed, not legally reviewed</span></p><p>Rule: <code>'
            f'{rule_id}</code></p><p>Duty as written: {duty}</p><p>Effective from <time datetime="{start}">{start}'
            f'</time> to {end}</p><p><a href="https://library.example.org/cedar/municode/5-1" rel="noopener norefer'
            'rer">Official source (organizer-recorded)</a> · checked <time datetime="2026-01-02">2026-01-02</time>'
            '</p></article>')


# Captured from main 26e2d09f (before slice 3) with finish tooling; the English build must keep these bytes exactly.
LOCAL_SECTION = (
    '<section class="local-rules"><h3>Local rules on record</h3><p>These are organizer transcriptions of local '
    "ordinances, not a determination about the agency's conduct.</p>"
    + _article("Cedar Light Code 2", "Not yet in force on build date", LIGHTING, "Log the lights.", "2022-01-01", "open")
    + _article("Cedar Municipal Code 5.1", "In force on build date", "local-us-ca-cedar-retention",
               "Delete plate reads after 30 days.", "2020-01-01", "open")
    + _article("Cedar Sign Code 9", "Past — not in force on build date", OTHER, "Post a sign.", "2020-01-01",
               '<time datetime="2020-12-31">2020-12-31</time>')
    + "</section>"
)


def override(key):
    """Override text for ``key``: a marker plus every placeholder its default allows (so values still render)."""
    return f"OVR{KEYS.index(key)}:" + "".join(" {" + name + "}" for name in sorted(ALLOWED[key]))


def fragments(key):
    """The literal English pieces of ``key``'s default (between placeholders) long enough to be distinctive."""
    return [piece.strip() for piece in PLACEHOLDER.split(DEFAULTS[key])[::2] if len(piece.strip()) >= 4]


def labels_file(root, labels):
    (root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")


def section(page, cls):
    start = page.find(f'<section class="{cls}">')
    return "" if start < 0 else page[start:page.index("</section>", start) + len("</section>")]


class KeysTests(unittest.TestCase):
    def test_new_keys_are_defaults_with_spanish_drafts(self):
        for key in KEYS:
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS)
                self.assertIn(key, ES, "examples/labels/es.json has no draft")
                self.assertEqual(set(PLACEHOLDER.findall(ES[key])), ALLOWED[key])


class LocalRulesLabelsTests(local_rules.LocalRulesCase):
    def test_english_build_unchanged(self):
        self.overlay(three_rules())
        self.assertEqual(self.section(self.agency_page(self.build())), LOCAL_SECTION)

    def test_every_key_overridden_leaves_no_english_default(self):
        self.overlay(three_rules())
        labels_file(self.root, {key: override(key) for key in LOCAL_KEYS})
        html = self.section(self.agency_page(self.build()))
        text = (html.replace('<section class="local-rules">', "").replace('<article class="local-rule">', "")
                .replace('rel="noopener noreferrer"', ""))  # (class and rel attributes name no label)
        for key in LOCAL_KEYS:
            with self.subTest(key=key):
                self.assertIn(f"OVR{KEYS.index(key)}:", html)
                for piece in fragments(key):
                    self.assertNotIn(piece, text)
        # values still render through the placeholders
        self.assertIn(f"OVR{KEYS.index('agency.local_rule_id')}: <code>{LIGHTING}</code>", html)
        self.assertIn(f"OVR{KEYS.index('agency.local_rule_effective')}: "
                      f"OVR{KEYS.index('agency.local_rule_open')}: "
                      '<time datetime="2022-01-01">2022-01-01</time>', html)

    def test_spanish_draft_builds(self):
        self.overlay(three_rules())
        labels_file(self.root, {key: ES[key] for key in LOCAL_KEYS})
        html = self.section(self.agency_page(self.build()))
        for english in ("Local rules on record", "organizer transcriptions", "In force on build date", "Past —",
                        "Not yet in force", "not legally reviewed", "Duty as written", "Effective from", " to open",
                        "Official source", "checked <time"):
            with self.subTest(english=english):
                self.assertNotIn(english, html)
        self.assertIn("Delete plate reads after 30 days.", html)  # the duty is shown as written


class CameraCountersLabelsTests(unittest.TestCase):
    # the cameras-page fixture (two cities, six points: Cedar 3, Pine 2, outside 1) without re-running its tests
    setUp, cameras, read = (cameras_page.CamerasPageTests.setUp, cameras_page.CamerasPageTests.cameras,
                            cameras_page.CamerasPageTests.read)
    write = staticmethod(cameras_page.CamerasPageTests.write)

    def counters(self):
        return dict(re.findall(r'<section class="camera-group"><h3>([^<]*)</h3><p>([^<]*)</p>', self.read("cameras.html")))

    def test_english_build_unchanged(self):
        build_site(self.root)
        self.assertEqual(self.counters(), {"Cedar": "3 mapped points", "Pine": "2 mapped points",
                                           cameras_page.GROUP: "1 mapped point"})
        self.assertNotIn("more on the map", self.read("cameras.html"))

    def big(self):
        self.cameras([point(0.6, 0.6 + i / 10000, manufacturer="Flock Safety", operator="Cedar PD", url=NODE + str(i))
                      for i in range(1, 2002)] + [point(1.2, 0.8, operator="Pine PD", url=NODE + "99999")])

    def test_more_line_unchanged_and_overridable(self):
        self.big()
        build_site(self.root)
        self.assertIn("<p>and 1501 more on the map</p>", self.read("cameras.html"))
        labels_file(self.root, {key: override(key) for key in CAMERA_KEYS})
        build_site(self.root)
        page = self.read("cameras.html")
        lines = self.counters()
        self.assertEqual(lines["Cedar"], f"OVR{KEYS.index('cameras.group_count_many')}: 2001")
        self.assertEqual(lines["Pine"], f"OVR{KEYS.index('cameras.group_count_one')}: 1")
        self.assertIn(f"<p>OVR{KEYS.index('cameras.group_more')}: 1501</p>", page)
        groups = "".join(re.findall(r'<section class="camera-group">.*?</section>', page))
        for key in CAMERA_KEYS:
            for piece in fragments(key):
                with self.subTest(key=key, piece=piece):
                    self.assertNotIn(piece, groups)


class SuspectedNoneLabelsTests(suspected_case.Case):
    def test_empty_line_unchanged_and_overridable(self):
        DAY = suspected_case.DAY
        self.put([])
        build_site(self.root, today=DAY)
        page = (self.root / "public" / "suspected.html").read_text(encoding="utf-8")
        self.assertIn("<p>No suspected locations are listed.</p>", page)
        labels_file(self.root, {"suspected.none": override("suspected.none")})
        build_site(self.root, today=DAY)
        page = (self.root / "public" / "suspected.html").read_text(encoding="utf-8")
        self.assertIn(f"<p>OVR{KEYS.index('suspected.none')}:</p>", page)
        self.assertNotIn("No suspected locations are listed.", page)
        labels_file(self.root, {"suspected.none": ES["suspected.none"]})
        build_site(self.root, today=DAY)
        self.assertIn(f"<p>{ES['suspected.none']}</p>",
                      (self.root / "public" / "suspected.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
