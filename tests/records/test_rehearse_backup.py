"""Runtime tests for the synthetic stage 19 rehearsal."""

import stat
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import rehearse_backup, run_stages


class RehearseBackupTests(unittest.TestCase):
    def test_C1_owner_only_marker(self):
        """C1: Mark the empty sandbox without exposing or replacing its marker."""
        # A default-mode or overwriting marker would fail this boundary test.
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            rehearse_backup._mark_sandbox(sandbox)
            marker = sandbox / run_stages.SANDBOX_MARKER
            self.assertEqual(list(sandbox.iterdir()), [marker])
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertIsNone(run_stages.sandbox_refusal(str(sandbox)))
            with self.assertRaises(FileExistsError) as refusal:
                rehearse_backup._mark_sandbox(sandbox)
            self.assertIs(type(refusal.exception), FileExistsError)
