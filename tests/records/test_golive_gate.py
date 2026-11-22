"""Runtime tests for the attended gate contract."""

import json
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class GateCliTests(unittest.TestCase):
    def test_c3_exclusive_private_receipt(self):
        """C3: --out creates a private receipt; a repeat cannot overwrite it."""
        # A normal JSON writer can expose the receipt or truncate prior evidence.
        with TemporaryDirectory(dir=Path.cwd().resolve()) as directory:
            root = Path(directory)
            evidence = root / "copy.dat"
            evidence.write_bytes(b"")
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "schema": "records-golive-gate-spec-v1",
                "gate_id": "fixture-gate",
                "stages": ["3"],
                "title": "Fixture",
                "checks": [{"type": "file_present", "label": "copy", "input": "copy"}],
            }), encoding="utf-8")
            out = root / "receipts"
            out.mkdir()
            command = [sys.executable, "-B", "-m", "campaign_tool.records.golive_gate",
                       "--spec", str(spec), "--input", f"copy={evidence}", "--out", str(out), "--json"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            receipt = out / "fixture-gate.json"
            original = receipt.read_bytes()
            self.assertEqual(original, result.stdout.encode("utf-8"))
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            repeated = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual((repeated.returncode, repeated.stdout), (2, ""))
            self.assertEqual(receipt.read_bytes(), original)
