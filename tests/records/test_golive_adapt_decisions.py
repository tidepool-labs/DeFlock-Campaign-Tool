"""Contract tests for the stage A5 (decisions applied) evidence adapter.

Every export here is built by ``console_export.build`` from a task-local project that holds real
``decisions-applied.jsonl`` receipts; only the anchor probe is mocked (no trust file, key or signature is used).
"""

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import approval_queue as aq, console_apply, console_export as ce
from campaign_tool.records import golive_adapt_decisions as adapter, golive_evidence
from tests.records.test_console_export import tree_state, unlock
from tests.records.test_pipeline_health import report_for

APPLIED, STALE = "APPLIED", "STALE_REVISION"
ID_A, ID_B = "a1" * 32, "b2" * 32
# Synthetic sensitive reason text: present in the source manifest, never in anything derived from it.
HIDDEN_REASON = "HIDDENZQ-REASON-TEXT"
OK_ANCHOR = {"state": "ok"}


class DecisionsEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.addCleanup(unlock, self.base)
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.report_path = self.base / "run-report.json"

    def make(self, receipts=(), anchor=OK_ANCHOR, edit=None, report_age=48):
        """A real export of a fresh project; ``receipts`` are (id, outcome) lines in the real receipt ledger."""
        self.report = report_for(self.now - timedelta(minutes=report_age))
        run = self.report["run_id"]
        self.report_path.write_text(json.dumps(self.report) + "\n", encoding="utf-8")
        project = self.base / "project"
        (project / "run-outputs" / run / "outcomes").mkdir(parents=True)
        (project / "run-outputs" / run / "outcomes" / "received-items.json").write_text('{"items": []}')
        (project / "run-outputs" / run / "text-state.json").write_text("{}")
        (project / "approvals").mkdir(mode=0o700)
        for ident, outcome in receipts:
            console_apply._receipt(str(project), ident, outcome)
        self.project = project
        self.root = self.base / "console-export"
        generation = Path("exports") / ("20261124T000000Z-" + run)
        (self.root / generation).mkdir(mode=0o700, parents=True)
        (self.root / "current").symlink_to(generation, target_is_directory=True)
        self.manifest = self.root / generation / ce.MANIFEST
        with patch.object(ce.approval, "anchor_state", return_value=anchor):
            ce.build(str(project), run, str(self.root / generation))
        if edit is not None:
            value = json.loads(self.manifest.read_bytes())
            edit(value)
            self.manifest.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def reset(self):
        shutil.rmtree(self.project)
        shutil.rmtree(self.root)

    def row(self):
        row = adapter.records(self.root, self.report_path, self.now + timedelta(minutes=1))
        self.assertIsNone(golive_evidence.validate(row))
        return row

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = adapter.main([*args])
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_c1_c2_healthy_control_passes_with_one_applied_entry_and_ok_anchor(self):
        """C1/C2: one APPLIED entry, ids and outcomes agreeing, anchor ok, bound manifest => pass row for A5.

        Believable bug: a wrong stage, kind or actor, or a row that fails golive_evidence.validate."""
        self.make([(ID_A, APPLIED)])
        manifest = json.loads(self.manifest.read_bytes())
        self.assertEqual((manifest["applied_journal_ids"], manifest["apply_outcomes"]),
                         ([ID_A], {ID_A: {"outcome": APPLIED}}))  # the export itself says one applied entry
        self.assertEqual(self.row(), {
            "schema": "records-golive-evidence-v1", "stage": "A5", "kind": "live", "result": "pass", "engine": None,
            "recorded_at": (self.now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"), "actor": "pipeline",
            "codes": ["DECISIONS_APPLIED", "APPROVAL_ANCHOR_OK"],
            "source": {"schema": ce.SCHEMA, "sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                       "name": ce.MANIFEST}})

    def test_c2_a_refusal_next_to_an_applied_entry_does_not_break_the_pass(self):
        """C2: an applied entry plus a refused one still passes (ids list only the applied one).

        Believable bug: demanding every outcome be APPLIED, so one refusal hides a real applied decision."""
        self.make([(ID_A, APPLIED), (ID_B, STALE)])
        manifest = json.loads(self.manifest.read_bytes())
        self.assertEqual(manifest["apply_outcomes"][ID_B], {"outcome": STALE, "refusal": STALE})
        self.assertEqual((self.row()["result"], self.row()["codes"]), ("pass", ["DECISIONS_APPLIED", "APPROVAL_ANCHOR_OK"]))

    def test_c3_refusals_only_or_no_receipts_is_none_applied(self):
        """C3: no APPLIED outcome (refusals only, or none recorded) fails DECISIONS_NONE_APPLIED.

        Believable bug: an entry with a refusal outcome counting as applied, or an empty list passing."""
        for receipts in ([(ID_A, STALE)], []):
            with self.subTest(receipts=len(receipts)):
                self.make(receipts)
                row = self.row()
                self.assertEqual((row["result"], row["codes"]), ("fail", ["DECISIONS_NONE_APPLIED"]))
                self.reset()

    def test_c3_applied_ids_and_outcomes_must_name_the_same_entries(self):
        """C3: an applied id without an APPLIED outcome, or the reverse, or a malformed shape, is inconsistent.

        Believable bug: trusting applied_journal_ids alone (or one APPLIED outcome alone) as proof."""
        def list_refused_id(value):
            value["applied_journal_ids"] = [ID_A, ID_B]

        def drop_listed_id(value):
            value["applied_journal_ids"] = []

        def refused_outcome_with_applied_word(value):
            value["apply_outcomes"][ID_A] = {"outcome": APPLIED, "refusal": STALE}

        def outcomes_not_a_mapping(value):
            value["apply_outcomes"] = [ID_A]

        def duplicate_listed_id(value):
            value["applied_journal_ids"] = [ID_A, ID_A]

        def refusal_missing_its_code(value):  # a non-APPLIED entry with no refusal field next to a valid APPLIED one
            value["apply_outcomes"][ID_B] = {"outcome": STALE}

        def refusal_code_differs_from_outcome(value):
            value["apply_outcomes"][ID_B] = {"outcome": STALE, "refusal": "SIGNATURE_REFUSED"}

        def refusal_with_extra_field(value):
            value["apply_outcomes"][ID_B] = {"outcome": STALE, "refusal": STALE, "note": "x"}

        def refusal_code_not_a_string(value):
            value["apply_outcomes"][ID_B] = {"outcome": STALE, "refusal": 7}

        for edit in (list_refused_id, drop_listed_id, refused_outcome_with_applied_word, outcomes_not_a_mapping,
                     duplicate_listed_id, refusal_missing_its_code, refusal_code_differs_from_outcome,
                     refusal_with_extra_field, refusal_code_not_a_string):
            with self.subTest(edit=edit.__name__):
                self.make([(ID_A, APPLIED), (ID_B, STALE)], edit=edit)
                row = self.row()
                self.assertEqual((row["result"], row["codes"]), ("fail", ["DECISIONS_OUTCOMES_INCONSISTENT"]))
                self.reset()

    def test_c3_manifest_not_bound_to_the_run_report_fails_even_with_an_applied_entry(self):
        """C3: a manifest for another run (stale or pending) or no manifest at all is DECISIONS_MANIFEST_UNBOUND.

        Believable bug: counting applied entries from an export that does not belong to the reported run."""
        for minutes in (60, 5):  # a newer run beyond the grace, and one inside it
            with self.subTest(minutes=minutes):
                self.make([(ID_A, APPLIED)])
                newer = report_for(self.now - timedelta(minutes=minutes), seq=1)
                self.report_path.write_text(json.dumps(newer) + "\n", encoding="utf-8")
                row = self.row()
                self.assertEqual((row["result"], row["codes"]), ("fail", ["DECISIONS_MANIFEST_UNBOUND"]))
                self.assertEqual(row["source"]["sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
                self.reset()
        self.make([(ID_A, APPLIED)])
        row = adapter.records(self.base / "no-such-export", self.report_path, self.now)  # report only: binds its bytes
        self.assertEqual((row["result"], row["codes"], row["source"]["schema"]),
                         ("fail", ["DECISIONS_MANIFEST_UNBOUND", "APPROVAL_ANCHOR_UNKNOWN"], "records-run-report-v1"))
        self.report_path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            adapter.records(self.base / "no-such-export", self.report_path, self.now)

    def test_c3_anchor_must_be_ok_even_when_a_decision_was_applied(self):
        """C3: unavailable or missing anchor state fails APPROVAL_ANCHOR_<STATE> next to an applied entry.

        Believable bug: ignoring the anchor, so an unverified approval looks live."""
        def drop_anchor(value):
            del value["approval_anchor"]

        for anchor, edit, state in (({"state": "unavailable", "reason": HIDDEN_REASON}, None, "UNAVAILABLE"),
                                    (OK_ANCHOR, drop_anchor, "UNKNOWN")):
            with self.subTest(state=state):
                self.make([(ID_A, APPLIED)], anchor=anchor, edit=edit)
                row = self.row()
                self.assertEqual((row["result"], row["codes"]), ("fail", ["APPROVAL_ANCHOR_" + state]))
                self.reset()

    def test_c3_every_failing_cause_is_listed(self):
        """C3: no applied entry and a bad anchor are both reported, in a fixed order.

        Believable bug: stopping at the first failing cause so the second stays hidden."""
        self.make([(ID_A, STALE)], anchor={"state": "unavailable"})
        self.assertEqual(self.row()["codes"], ["DECISIONS_NONE_APPLIED", "APPROVAL_ANCHOR_UNAVAILABLE"])

    def test_c4_output_carries_no_ids_signatures_keys_or_text(self):
        """C4: the row, codes and CLI output are counts/codes only; the source is the manifest bytes.

        Believable bug: echoing a journal id or the anchor's reason text into codes or the printed line."""
        self.make([(ID_A, APPLIED), (ID_B, STALE)], anchor={"state": "unavailable", "reason": HIDDEN_REASON},
                  edit=lambda value: value.update(note="-----BEGIN SSH SIGNATURE----- decision text"))
        outputs = [json.dumps(self.row())]
        for flags in ((), ("--json",)):
            code, out, err = self.cli("--export-root", str(self.root), "--run-report", str(self.report_path), *flags)
            self.assertEqual(code, 1)
            outputs += [out, err]
        self.assertIn(HIDDEN_REASON, self.manifest.read_text())  # the sensitive text exists in the source ...
        for text in outputs:
            for hidden in (ID_A, ID_B, HIDDEN_REASON, "SSH SIGNATURE", "decision text"):
                self.assertNotIn(hidden, text)  # ... and never in anything derived from it
        self.assertEqual(self.row()["source"], {"schema": ce.SCHEMA, "name": ce.MANIFEST,
                                                "sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest()})

    def test_c5_reading_writes_nothing_to_the_export_or_the_project(self):
        """C5: records() and the CLI leave every path, mode, size, mtime and byte of both trees unchanged.

        Believable bug: repairing or caching a receipt next to the export, or touching the ledger."""
        self.make([(ID_A, APPLIED)])
        before = tree_state(self.base)
        self.row()
        self.assertEqual(self.cli("--export-root", str(self.root), "--run-report", str(self.report_path))[0], 0)
        self.assertEqual(tree_state(self.base), before)

    def test_c1_cli_exit_codes_pass_fail_and_bad_input(self):
        """C1: exit 0 only on pass, 1 on a fail row, 2 with a stable code (no traceback) on bad input.

        Believable bug: a fail row exiting 0, or an unreadable input raising instead of exiting 2."""
        self.make([(ID_A, APPLIED)])
        args = ("--export-root", str(self.root), "--run-report", str(self.report_path))
        code, out, err = self.cli(*args, "--json")
        self.assertEqual((code, err, json.loads(out)["result"]), (0, "", "pass"))
        self.assertEqual(self.cli(*args)[:2], (0, "A5 pass DECISIONS_APPLIED APPROVAL_ANCHOR_OK\n"))
        value = json.loads(self.manifest.read_bytes())
        value["applied_journal_ids"] = []
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.cli(*args, "--json")[0], 1)
        self.report_path.write_text("{", encoding="utf-8")
        self.assertEqual(self.cli("--export-root", str(self.base / "empty"), "--run-report", str(self.report_path)),
                         (2, "", adapter.BAD_INPUT + "\n"))


if __name__ == "__main__":
    unittest.main()
