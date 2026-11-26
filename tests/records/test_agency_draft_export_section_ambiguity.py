"""Section selection through the real exporter, using only synthetic drafts."""
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import agency_draft_export as ade
from tests.records.test_agency_draft_export import entry, mapping


def section(heading, body):
    return f"{heading}\n\n> Subject: Follow-up\n>\n> {body}\n\n"


class SectionAmbiguityTests(unittest.TestCase):
    def export(self, markdown):
        tmp = tempfile.TemporaryDirectory(prefix="draft-section-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        md, source, project, out = (root / name for name in ("draft.md", "map.json", "project", "out"))
        md.write_bytes(markdown.encode("utf-8"))
        source.write_text(json.dumps(mapping(entry(section="Reply", subject="Follow-up", placeholders=()))),
                          encoding="utf-8")
        project.mkdir(mode=0o700)
        return ade.export(md, source, project, out), out

    def assert_refused(self, markdown, code):
        report, out = self.export(markdown)
        self.assertEqual(report["refused"], [{"draft_key": "letter-1", "code": code}])
        self.assertFalse(report["ok"])
        self.assertEqual(report["files"], [])
        self.assertEqual(list(out.iterdir()), [])

    def test_duplicate_exact_headings_with_same_subject_refuse(self):
        # A subject check alone accepts the wrong body if selection takes the first match.
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                markdown = section("## Reply", "First letter.") + section("## Reply", "Second letter.")
                self.assert_refused(markdown.replace("\n", newline), "section_ambiguous")

    def test_unique_exact_heading_ignores_similarly_named_headings(self):
        # Prefix or case-insensitive matching would refuse or pick a neighboring letter.
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                markdown = (section("## Reply later", "Prefix match only.")
                            + section("## Reply", "Exact match.")
                            + section("## reply", "Case differs."))
                report, out = self.export(markdown.replace("\n", newline))
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["refused"], [])
                path = out / "letter-1.json"
                self.assertEqual(report["files"], [str(path)])
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["body"], "Exact match.\n")

    def test_missing_exact_heading_among_near_matches_stays_unknown(self):
        # Near matches alone must not be reported as ambiguous or selected as a fallback.
        markdown = section("## Reply later", "Prefix match only.") + section("## reply", "Case differs.")
        self.assert_refused(markdown, "section_unknown")
