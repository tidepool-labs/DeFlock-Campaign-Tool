"""Synthetic stage 19 recovery rehearsal (scope checkpoint)."""

import os

from .run_stages import SANDBOX_MARKER

STAGES = ("19",)
TITLE = "Backup, verify and isolated restore"


def _mark_sandbox(sandbox):
    """Create the owner-only marker before seeding any inputs."""
    fd = os.open(sandbox / SANDBOX_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
