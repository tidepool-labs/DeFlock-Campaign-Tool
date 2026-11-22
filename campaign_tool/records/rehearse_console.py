"""Synthetic evidence for the loopback console rehearsal (A3)."""

import io
import os
from unittest.mock import patch

from . import console_export, run_stages

STAGES = ("A3",)
TITLE = "New console on loopback"
_RUN_ID = "run_20200101T000000Z_00000001"


def _build_export(sandbox):
    marker = sandbox / run_stages.SANDBOX_MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write('{"sandbox_marker_version":1}\n')

    project = sandbox / "project"
    run = project / "run-outputs" / _RUN_ID
    run.mkdir(parents=True, mode=0o700)
    (run / "text-state.json").write_text("{}\n", encoding="utf-8")
    build = sandbox / "build"
    build.mkdir(mode=0o700)
    # The ordinary export probes the host approval anchor even for an empty
    # project. This synthetic console has no approval trust configured.
    with patch.object(console_export.approval, "anchor_state",
                      return_value={"state": "unavailable", "reason": "synthetic_rehearsal"}):
        console_export.build(project, _RUN_ID, build)

    root = sandbox / "console"
    root.mkdir(mode=0o700)
    stream = io.BytesIO()
    console_export.write_tar(build, stream)
    stream.seek(0)
    name = console_export.install(str(root), stream)
    return root, root / "exports" / name / console_export.MANIFEST
