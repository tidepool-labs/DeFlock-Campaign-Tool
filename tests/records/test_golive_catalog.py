import io
import json
import subprocess
import sys
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError

from campaign_tool.records import golive_catalog, golive_stages


# Independent expected rows: catch a dropped/moved stage, relabeling, or drift
# in a gate, rehearsal, source or owner (especially the v1 proof exceptions).
EXPECTED_TEXT = """\
Stage | Label | Pipeline | Rehearsal | Attended gate | Live source | Owner
1 | Request sent | yes | no | no | evidence | G
2 | Email intake / sync | yes | yes | no | run_report | pipeline
3 | Arrival trigger | yes | yes | yes | evidence | operator
4 | Portal downloads | yes | yes | yes | evidence | operator
5 | Save + extract text | yes | yes | no | run_report | flock-coordinator
6 | OCR | yes | yes | no | evidence | flock-coordinator
7 | Catalogue / adoption | yes | yes | no | run_report | pipeline
8 | Digest gates | yes | yes | no | run_report | pipeline
9 | Facts | yes | yes | no | run_report | pipeline
10 | Policy comparison | yes | yes | no | run_report | pipeline
11 | Gap proposals | yes | yes | no | run_report | pipeline
12 | Received items, timeline, register | yes | yes | no | run_report | pipeline
13 | Reply drafts to approval queue | yes | yes | no | run_report | pipeline
14 | No-response follow-up drafts | yes | yes | no | run_report | pipeline
15 | Engine-owned follow-ups | yes | yes | yes | evidence | operator
16 | Findings lane | yes | yes | yes | evidence | operator
17 | Packets to website staging | yes | yes | yes | evidence | operator
17b | Public site | yes | no | no | evidence | operator
18 | Timers + monitoring | yes | yes | yes | evidence | operator
19 | Backups | yes | yes | yes | evidence | operator
20 | Approval trust anchor | yes | yes | yes | evidence | operator
A1 | Console on the box | no | yes | no | evidence | operator
A2 | Console export | yes | yes | no | evidence | pipeline
A3 | New console | no | yes | yes | evidence | operator
A4 | Console access | no | yes | yes | evidence | G
A5 | Decisions applied | no | yes | yes | evidence | operator
A6 | Phone signing | no | yes | yes | evidence | operator
A7 | Workers console / shim | no | yes | no | evidence | operator
"""


def expected_rows():
    rows = {}
    for line in EXPECTED_TEXT.splitlines()[1:]:
        stage, label, pipeline, rehearsal, gate, source, owner = line.split(" | ")
        rows[stage] = dict(label=label, pipeline=pipeline == "yes", needs_rehearsal=rehearsal == "yes",
                           needs_gate=gate == "yes", live_source=source, owner=owner)
    return rows


class GoLiveCatalogueTests(unittest.TestCase):
    def test_c1_ordered_mapping_and_frozen_row(self):
        """C1: Consumers get an ordered mapping with immutable Row values."""
        self.assertIsInstance(golive_catalog.STAGES, Mapping)
        self.assertEqual(tuple(golive_catalog.STAGES), tuple(expected_rows()))
        with self.assertRaises(FrozenInstanceError) as error:
            setattr(golive_catalog.STAGES["1"], "label", "Changed")
        self.assertIs(type(error.exception), FrozenInstanceError)

    def test_c2_pipeline_membership_and_report_labels(self):
        """C2: A console-only row cannot count and a report label cannot drift."""
        self.assertEqual({stage for stage, row in golive_catalog.STAGES.items() if row.pipeline},
                         {str(stage) for stage in range(1, 21)} | {"17b", "A2"})
        self.assertEqual({stage: row.label for stage, row in golive_catalog.STAGES.items() if stage != "17b"},
                         {stage: row.label for stage, row in golive_stages.STAGES.items()})

    def test_c3_whole_row_matrix(self):
        """C3: Pin every row's complete value, including C1 fields and C2 labels."""
        for stage, fields in expected_rows().items():
            with self.subTest(stage=stage):
                self.assertEqual(golive_catalog.STAGES[stage], golive_catalog.Row(**fields))

    def test_c4_text_cli(self):
        """C4: The module entry point prints the complete human-readable table."""
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_catalog"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, EXPECTED_TEXT, ""))

    def test_c4_json_cli(self):
        """C4: JSON callers receive ordered stage IDs and typed row fields, not text cells."""
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_catalog", "--json"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stderr, json.loads(result.stdout, object_pairs_hook=list)),
                         (0, "", [(stage, list(fields.items())) for stage, fields in expected_rows().items()]))

    def test_c4_unknown_argument(self):
        """C4: Unknown options are rejected rather than yielding a successful table."""
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                golive_catalog.main(["--not-an-option"])
        self.assertEqual((type(error.exception), error.exception.code, stdout.getvalue()), (SystemExit, 2, ""))
