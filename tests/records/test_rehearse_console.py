"""Runtime coverage for the A3 console rehearsal."""

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool import approval
from campaign_tool.records import console_check, console_export, rehearse_console, run_stages


class RehearseConsoleTests(unittest.TestCase):
    def test_c1_build_export(self):
        """C1: The fixture installs a fresh export without consulting the host anchor."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            sandbox = Path(tmp).resolve()
            marker = sandbox / run_stages.SANDBOX_MARKER
            build = console_export.build

            def after_marker(*args):
                self.assertEqual(marker.read_bytes(), b'{"sandbox_marker_version":1}\n')
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
                return build(*args)

            # Detect an unmarked build, a live trust probe or a fixture that
            # writes a manifest but never makes it readable by the console.
            with patch.object(console_export, "build", side_effect=after_marker), \
                    patch.object(approval, "anchor_status", side_effect=AssertionError("host approval probe")):
                root, manifest = rehearse_console._build_export(sandbox)
            self.assertEqual(manifest, (root / "current").resolve() / "EXPORT.json")
            self.assertEqual(json.loads(manifest.read_text())["approval_anchor"],
                             {"state": "unavailable", "reason": "synthetic_rehearsal"})
            self.assertEqual(console_check._export_check(root, 36), "PASS")
