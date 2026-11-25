"""Two-version policy reading worksheet: byte-bound passages per revision, withheld sides and refused swaps."""
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "records" / "policy_version_worksheet.py"
_spec = importlib.util.spec_from_file_location("policy_version_worksheet", SCRIPT)
assert _spec is not None and _spec.loader is not None
pvw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pvw)

AGENCY, POLICY, FIELD = "synthetic-two-revisions", "policy-A", "retention"
PRIOR_TEXT = "The system retains records for 30 days."
CURRENT_TEXT = "The system retains records for 45 days."


def source(revision, effective, pages, agency=AGENCY, policy=POLICY):
    doc = {"agency": agency, "policy": policy, "revision": revision, "pages": pages}
    if effective is not None:
        doc["effective"] = effective
    return (json.dumps(doc, sort_keys=True) + "\n").encode()


class Case:
    """A public synthetic pair (the D30/D44 shape) written to a temporary folder."""

    def __init__(self, tmp, current_text=CURRENT_TEXT):
        self.dir = Path(tmp)
        self.files = {"rev-a.json": source("rev-A", "2025-01-01", [{FIELD: PRIOR_TEXT}]),
                      "rev-b.json": source("rev-B", "2026-06-01", [{}, {}, {FIELD: current_text}])}
        self.pair = {"schema": pvw.PAIR_SCHEMA, "agency": AGENCY, "policy": POLICY, "field": FIELD,
                     "prior_revision": "rev-A", "current_revision": "rev-B",
                     "revisions": [self.row("rev-A", "rev-a.json", 1, PRIOR_TEXT),
                                   self.row("rev-B", "rev-b.json", 3, current_text)]}

    def row(self, revision, name, page, passage):
        return {"revision": revision, "source": name, "page": page, "passage": passage,
                "source_sha256": hashlib.sha256(self.files[name]).hexdigest()}

    def write(self):
        for name, raw in self.files.items():
            (self.dir / name).write_bytes(raw)
        path = self.dir / "pair.json"
        path.write_text(json.dumps(self.pair), encoding="utf-8")
        return path

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.dir.iterdir())}


class WorksheetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="policy-worksheet-")
        self.case = Case(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def sheet(self):
        return pvw.build(self.case.write())

    def test_both_revisions_keep_their_own_passage_locator_and_source_hash(self):
        sheet = self.sheet()
        self.assertEqual(sheet["wording"], pvw.DIFFERENT)
        prior, current = sheet["prior"], sheet["current"]
        self.assertEqual((prior["revision"], prior["locator"], prior["passage"]), ("rev-A", "page 1", PRIOR_TEXT))
        self.assertEqual((current["revision"], current["locator"], current["passage"]), ("rev-B", "page 3", CURRENT_TEXT))
        self.assertEqual(prior["source_sha256"], hashlib.sha256(self.case.files["rev-a.json"]).hexdigest())
        self.assertEqual(current["source_sha256"], hashlib.sha256(self.case.files["rev-b.json"]).hexdigest())
        self.assertEqual(sheet["human"], {"reviewer": "", "read_on": "", "finding": ""})
        self.assertIs(sheet["review_required"], True)
        self.assertIs(sheet["policy_conclusion"], False)

    def test_same_wording_is_not_a_change(self):
        self.case = Case(self._tmp.name, current_text=PRIOR_TEXT)
        self.assertEqual(self.sheet()["wording"], pvw.SAME)

    def test_absent_or_disputed_text_is_withheld_without_leaking(self):
        # A withheld side keeps only its ID and reason; the other side stays readable.
        rows = self.case.pair["revisions"]
        cases = {
            "REVISION_ABSENT": lambda: rows.pop(),
            "DISPUTED_DUPLICATE_ROWS": lambda: rows.append(dict(rows[1], passage="The system retains records for 60 days.")),
            "DISPUTED_PASSAGE": lambda: rows[1].update(disputed=True),
            "PASSAGE_NOT_ON_CITED_PAGE": lambda: rows[1].update(page=2),
            "LOCATOR_INVALID": lambda: rows[1].update(page=4),
            "SOURCE_BYTES_CHANGED": lambda: self.case.files.update(
                {"rev-b.json": source("rev-B", "2026-06-01", [{}, {}, {FIELD: "edited"}])}),
            "SOURCE_UNAVAILABLE": lambda: rows[1].update(source="missing.json"),
            "SOURCE_REFERENCE_INVALID": lambda: rows[1].update(source="../rev-b.json"),
            "CROSSED_SOURCE": lambda: self.case.files.update(
                {"other.json": source("rev-B", "2026-06-01", [{}, {}, {FIELD: CURRENT_TEXT}], agency="other-agency")})
            or rows[1].update(source="other.json",
                              source_sha256=hashlib.sha256(self.case.files["other.json"]).hexdigest()),
            "EFFECTIVE_DATE_MISSING": lambda: self.case.files.update(
                {"rev-b.json": source("rev-B", None, [{}, {}, {FIELD: CURRENT_TEXT}])})
            or rows[1].update(source_sha256=hashlib.sha256(self.case.files["rev-b.json"]).hexdigest()),
        }
        for reason, mutate in cases.items():
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory(prefix="policy-worksheet-") as tmp:
                    self.case = Case(tmp)
                    rows = self.case.pair["revisions"]
                    mutate()
                    sheet = pvw.build(self.case.write())
                self.assertEqual(sheet["wording"], pvw.UNREVIEWED)
                self.assertEqual(sheet["current"], {"revision": "rev-B", "withheld": reason})
                self.assertEqual(sheet["prior"]["passage"], PRIOR_TEXT)
                text = json.dumps(sheet) + pvw.render(sheet)
                self.assertNotIn("45 days", text)
                self.assertNotIn("60 days", text)
                self.assertNotIn("page 3", text)

    def test_identical_agency_with_swapped_revisions_is_refused(self):
        rows = self.case.pair["revisions"]
        # 1) The pair calls the newer revision "prior".
        self.case.pair.update(prior_revision="rev-B", current_revision="rev-A")
        with self.assertRaisesRegex(pvw.Refused, "not older"):
            self.sheet()
        # 2) Each row carries the other revision's source bytes.
        self.case.pair.update(prior_revision="rev-A", current_revision="rev-B")
        rows[0]["source"], rows[1]["source"] = rows[1]["source"], rows[0]["source"]
        rows[0]["source_sha256"], rows[1]["source_sha256"] = rows[1]["source_sha256"], rows[0]["source_sha256"]
        with self.assertRaisesRegex(pvw.Refused, "swapped"):
            self.sheet()
        # 3) Same revision ID twice.
        self.case.pair.update(current_revision="rev-A")
        with self.assertRaisesRegex(pvw.Refused, "same ID"):
            self.sheet()

    def test_swapped_sources_are_refused_even_when_a_row_is_disputed_or_duplicated(self):
        # Review t_ef131f44 item 1: a disputed or duplicate row must not hide independently verifiable swapped sources.
        def swapped(tmp):
            case = Case(tmp)
            rows = case.pair["revisions"]
            rows[0]["source"], rows[1]["source"] = rows[1]["source"], rows[0]["source"]
            rows[0]["source_sha256"], rows[1]["source_sha256"] = rows[1]["source_sha256"], rows[0]["source_sha256"]
            return case, rows
        variants = {
            "prior disputed": lambda rows: rows[0].update(disputed=True),
            "current disputed": lambda rows: rows[1].update(disputed=True),
            "both disputed": lambda rows: (rows[0].update(disputed=True), rows[1].update(disputed=True)),
            "prior duplicated": lambda rows: rows.append(dict(rows[0], passage="other")),
            "current duplicated": lambda rows: rows.append(dict(rows[1], passage="other")),
        }
        for name, mutate in variants.items():
            with self.subTest(variant=name):
                with tempfile.TemporaryDirectory(prefix="policy-worksheet-") as tmp:
                    case, rows = swapped(tmp)
                    mutate(rows)
                    path = case.write()
                    with self.assertRaisesRegex(pvw.Refused, "swapped"):
                        pvw.build(path)
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        self.assertEqual(pvw.main([str(path)]), 2)
                    self.assertEqual(out.getvalue(), "")
        # Control: only one direction crossed (current row carries the prior source, prior row disputed but its own
        # source) is not a swap; the current side is withheld and the sheet stays UNREVIEWED.
        rows = self.case.pair["revisions"]
        rows[0]["disputed"] = True
        rows[1].update(source=rows[0]["source"], source_sha256=rows[0]["source_sha256"])
        sheet = self.sheet()
        self.assertEqual(sheet["wording"], pvw.UNREVIEWED)
        self.assertEqual(sheet["prior"], {"revision": "rev-A", "withheld": "DISPUTED_PASSAGE"})
        self.assertEqual(sheet["current"], {"revision": "rev-B", "withheld": "SOURCE_NAMES_OTHER_REVISION"})

    def test_non_string_source_revision_is_withheld_not_a_crash(self):
        # Review t_bb688080 item 1: a byte-verified source whose revision is an array/object must not raise TypeError,
        # including behind a disputed or duplicated row.
        for bad in (["rev-B"], {"id": "rev-B"}, 7, None):
            for variant in ("plain", "disputed", "duplicated"):
                with self.subTest(bad=bad, variant=variant):
                    with tempfile.TemporaryDirectory(prefix="policy-worksheet-") as tmp:
                        case = Case(tmp)
                        case.files["odd.json"] = source(bad, "2026-06-01", [{}, {}, {FIELD: CURRENT_TEXT}])
                        rows = case.pair["revisions"]
                        rows[1].update(source="odd.json",
                                       source_sha256=hashlib.sha256(case.files["odd.json"]).hexdigest())
                        if variant == "disputed":
                            rows[1]["disputed"] = True
                        elif variant == "duplicated":
                            rows.append(dict(rows[1]))
                        sheet = pvw.build(case.write())
                    expected = {"plain": "SOURCE_NAMES_OTHER_REVISION", "disputed": "DISPUTED_PASSAGE",
                                "duplicated": "DISPUTED_DUPLICATE_ROWS"}[variant]
                    self.assertEqual(sheet["wording"], pvw.UNREVIEWED)
                    self.assertEqual(sheet["current"], {"revision": "rev-B", "withheld": expected})
                    self.assertEqual(sheet["prior"]["passage"], PRIOR_TEXT)

    def test_same_effective_date_is_refused(self):
        self.case.files["rev-b.json"] = source("rev-B", "2025-01-01", [{}, {}, {FIELD: CURRENT_TEXT}])
        self.case.pair["revisions"][1]["source_sha256"] = hashlib.sha256(self.case.files["rev-b.json"]).hexdigest()
        with self.assertRaisesRegex(pvw.Refused, "not older"):
            self.sheet()

    def test_malformed_pair_is_refused(self):
        for patch in ({"schema": "other"}, {"field": ""}, {"revisions": "x"}, {"agency": None}):
            with self.subTest(patch=patch):
                self.case.pair = dict(Case(self._tmp.name).pair, **patch)
                with self.assertRaises(pvw.Refused):
                    self.sheet()
        bad = Path(self._tmp.name) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(pvw.Refused):
            pvw.build(bad)

    def test_cli_exit_codes_text_and_no_writes(self):
        path = self.case.write()
        before = self.case.snapshot()
        out = subprocess.run([sys.executable, str(SCRIPT), str(path), "--format", "text"],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Cited difference", out.stdout)
        self.assertIn("page 3 of rev-b.json", out.stdout)
        self.assertIn("Finding: ____", out.stdout)
        self.assertEqual(self.case.snapshot(), before)
        self.case.pair["revisions"].pop()
        path = self.case.write()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(pvw.main([str(path)]), 1)
        self.assertEqual(json.loads(buf.getvalue())["current"]["withheld"], "REVISION_ABSENT")
        self.case.pair.update(current_revision="rev-A")
        path = self.case.write()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(pvw.main([str(path)]), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("refused", err.getvalue())

    def test_symlinked_source_is_not_followed(self):
        path = self.case.write()
        real = self.case.dir / "rev-b.json"
        os.replace(real, self.case.dir / "elsewhere.json")
        os.symlink(self.case.dir / "elsewhere.json", real)
        self.assertEqual(pvw.build(path)["current"]["withheld"], "SOURCE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
