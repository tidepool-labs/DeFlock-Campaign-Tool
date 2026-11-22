"""Synthetic stage-4 portal rehearsal (sandbox marker checkpoint)."""

import os
from pathlib import Path

from . import rehearsal_trust, run_stages

STAGES = ("4",)
TITLE = "Portal downloads"


def _mark_sandbox(sandbox: Path) -> Path:
    sandbox = rehearsal_trust.sandbox_path(sandbox)
    rehearsal_trust.require_private(sandbox)
    if any(sandbox.iterdir()):
        raise ValueError("sandbox must be empty")
    marker = sandbox / run_stages.SANDBOX_MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return marker
