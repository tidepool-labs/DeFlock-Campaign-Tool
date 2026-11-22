"""Runtime proof for the stage-4 sandbox marker checkpoint."""

import stat
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import rehearse_portal, run_stages


class PortalRehearsalTests(unittest.TestCase):
    def test_c2_private_empty_sandbox_marker(self):
        """C2: mark only the empty private sandbox; never overwrite its marker."""
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as tmp:
            sandbox = Path(tmp)
            marker = sandbox / run_stages.SANDBOX_MARKER
            self.assertEqual(rehearse_portal._mark_sandbox(sandbox), marker)
            self.assertEqual(list(sandbox.iterdir()), [marker])
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual(marker.read_bytes(), b"")
            with self.assertRaises(ValueError) as refusal:
                rehearse_portal._mark_sandbox(sandbox)
            self.assertIs(type(refusal.exception), ValueError)
            self.assertEqual(refusal.exception.args, ("sandbox must be empty",))
            self.assertEqual(marker.read_bytes(), b"")
