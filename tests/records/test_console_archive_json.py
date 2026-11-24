"""Run history must reject ambiguous or misencoded JSON without dropping healthy reports."""
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import console_runs_model as model
from tests.records.test_console_runs import NOW, make


class ArchiveJSON(unittest.TestCase):
    # Plain json.loads silently keeps the last value; checking only the root would still miss a duplicate stage field.
    def test_invalid_json_skips_the_entry_not_its_healthy_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "t"
            reports = target / "reports"
            reports.mkdir(parents=True)
            path = str(target / "run-report.json")
            older, candidate, newer = make(1), make(2), make(3, "fail")
            for row in (older, candidate, newer):
                (reports / (row["run_id"] + ".json")).write_text(json.dumps(row), encoding="utf-8")

            self.assertEqual(model.load_history(path, NOW), {
                "state": "ok", "code": None, "skipped": 0, "reports": [newer, candidate, older],
            })
            raw = json.dumps(candidate)
            variants = {
                "root duplicate": raw.replace(
                    '"overall_status": "ok"', '"overall_status": "fail", "overall_status": "ok"', 1,
                ).encode("utf-8"),
                "nested duplicate": raw.replace(
                    '"status": "ok"', '"status": "fail", "status": "ok"', 1,
                ).encode("utf-8"),
                # Passing bytes to json.loads would autodetect UTF-16 and admit this report.
                "UTF-16": raw.encode("utf-16"),
            }
            for variant, encoded in variants.items():
                with self.subTest(variant=variant):
                    self.assertNotEqual(encoded, raw.encode("utf-8"))
                    (reports / (candidate["run_id"] + ".json")).write_bytes(encoded)
                    self.assertEqual(model.load_history(path, NOW), {
                        "state": "ok", "code": None, "skipped": 1, "reports": [newer, older],
                    })
