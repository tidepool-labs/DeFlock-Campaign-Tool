"""DF1125-07: the campaign's own public tallies say when they were counted (synthetic temp copies; no network)."""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.labels import DEFAULTS
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
ES = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
TODAY = date(2026, 11, 1)
BUILT = '<time datetime="2026-11-01">2026-11-01</time>'
KEYS = ("standings.as_of", "standings.map_as_of", "standings.map_date_unknown", "agencies.glance_asked_counted")
STRIP = re.compile(r'<section class="section" id="standings">(.*?)</section>', re.S)
CAMERA_ROW = re.compile(r'<li><a href="/cameras.html">[^<]*</a>: <strong>[^<]*</strong>(.*?)</li>')
GLANCE_NOTE = re.compile(r'<section class="at-a-glance" id="at-a-glance"><h3>[^<]*</h3><p>(.*?)</p><table')


class DatedTalliesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-dated-tallies-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"

    def snapshot(self, osm_base):
        doc = {"schema_version": 1, "osm_base": osm_base, "source": "saved_file"}
        (self.content / "map" / "snapshot.json").write_text(json.dumps(doc), encoding="utf-8")

    def build(self, labels=None):
        if labels is not None:
            (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        build_site(self.root, today=TODAY)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def strip(self):
        match = STRIP.search(self.read("index.html"))
        self.assertIsNotNone(match, "no standings strip")
        return match.group(1) if match else ""

    def camera_row(self):
        rows = CAMERA_ROW.findall(self.strip())
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_home_counts_dated_with_map_date_never_build_date(self):
        self.snapshot("2026-10-20T03:04:05Z")
        self.build()
        strip = self.strip()
        self.assertIn(f'</ul><p class="counts-as-of">Counts as of {BUILT}.</p>', strip)
        self.assertEqual(self.camera_row(),
                         ' <small class="map-as-of">(map data as of <time datetime="2026-10-20">2026-10-20</time>)</small>')
        self.assertEqual(strip.count("map-as-of"), 1)  # only the camera-points row carries the map date
        self.assertNotIn("2026-11-01", self.camera_row())

    def test_home_map_date_unknown_without_snapshot_or_map(self):
        self.assertFalse((self.content / "map" / "snapshot.json").exists())
        self.build()
        self.assertEqual(self.camera_row(), ' <small class="map-as-of">(map date unknown)</small>')
        self.assertIn(f"Counts as of {BUILT}.", self.strip())
        self.snapshot(None)  # a null osm_base is an unknown day too
        self.build()
        self.assertEqual(self.camera_row(), ' <small class="map-as-of">(map date unknown)</small>')
        shutil.rmtree(self.content / "map")
        self.build()
        self.assertEqual(self.camera_row(), ' <small class="map-as-of">(map date unknown)</small>')
        self.assertNotIn("map data as of", self.strip())

    def test_glance_note_names_our_own_count_date(self):
        self.build()
        note = GLANCE_NOTE.search(self.read("agencies.html"))
        self.assertIsNotNone(note)
        self.assertEqual(note.group(1) if note else "", DEFAULTS["agencies.glance_note"] + f" Records items asked is our own count on {BUILT}.")

    def test_relabelled_override_replaces_every_new_text(self):
        self.snapshot("2026-10-20T03:04:05Z")
        self.build({key: f"X{key}X {{date}}" if "{date}" in DEFAULTS[key] else f"X{key}X" for key in KEYS})
        home, agencies = self.read("index.html"), self.read("agencies.html")
        self.assertIn(f"Xstandings.as_ofX {BUILT}", home)
        self.assertIn('Xstandings.map_as_ofX <time datetime="2026-10-20">', home)
        self.assertIn(f"Xagencies.glance_asked_countedX {BUILT}", agencies)
        for text in ("Counts as of", "map data as of", "is our own count on"):
            self.assertNotIn(text, home + agencies)
        shutil.rmtree(self.content / "map")
        self.build()
        self.assertIn("Xstandings.map_date_unknownX", self.read("index.html"))
        self.assertNotIn("map date unknown", self.read("index.html"))
        for key in KEYS:  # the Spanish draft has each key with the same placeholders
            self.assertIn(key, ES)
            self.assertEqual(re.findall(r"\{[a-z_]+\}", ES[key]), re.findall(r"\{[a-z_]+\}", DEFAULTS[key]), key)

    def test_empty_cases_add_nothing(self):
        (self.content / "agencies.json").write_text("[]", encoding="utf-8")
        shutil.rmtree(self.content / "map")  # the map config names agency slugs
        self.build()
        self.assertNotIn("at-a-glance", self.read("agencies.html"))
        self.assertNotIn("our own count", self.read("agencies.html"))
        self.assertIn("Counts as of", self.strip())  # the home strip is always shown on a content-backed home
        shutil.rmtree(self.content)  # no content: the neutral starter, which has no strip
        shutil.rmtree(self.public)
        build_site(self.root, today=TODAY)
        starter = self.read("index.html")
        self.assertNotIn('id="standings"', starter)
        self.assertNotIn("Counts as of", starter)


if __name__ == "__main__":
    unittest.main()
