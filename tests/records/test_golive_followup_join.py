"""golive_followup_join (DFS1124-0921-12, stage 14): one (queue id, revision) is a real no-response follow-up draft in ONE pinned export.

Fixtures are synthetic: a private temp project and root built by the repo's own export writer (Panel.make_export), so
the join reads exactly what a real sealed export holds. Numbered contracts C1-C4 are the ones in the card."""
import hashlib
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_export as ce, golive_followup_join as gfj, mail_drafts
from tests.records import test_console_snapshot as tcs
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN

BODY, FOLDER, MSGID = "SENTINEL-BODY-TEXT", "SENTINEL-FOLDER", "<sentinel-id@example.invalid>"
OUT_KEYS = {"code", "export", "exported_at", "kind", "manifest_sha256", "queue_id", "revision", "run_id", "state"}
FOLLOWUPS = (("reminder", "reminder-no-response", "placed"), ("promised", "reminder-promised-date", "found_existing"),
             ("notice", "formal-notice-no-response", "placed"), ("engine", "followup-playbook", "placed"))


def stamp(delta):
    return (datetime.now(timezone.utc) + delta).strftime(gfj.STAMP)


class Join(tcs.Panel):
    def setUp(self):
        super().setUp()
        self.n, self.journal = 0, [{"queue_id": self.approved_id, "revision": 1, "state": "placed"}]
        # the signed, placed reply-extension (revision 1) is the reply control; the pending one was never placed
        self.ids = {"reply": self.approved_id, "reply_unplaced": self.pending_id}
        for key, kind, state in FOLLOWUPS + (("appeal", "appeal-denial", "placed"), ("fee", "fee-reply", "found_existing"),
                                             ("first", "first-request", "placed"),
                                             ("missing", "followup-missing-records", "placed"),
                                             ("decision", "draft-agency-summary", "placed"),
                                             ("failed", "reminder-no-response", "failed"),
                                             ("unplaced", "reminder-no-response", None),
                                             ("stale", "reminder-no-response", "placed")):
            self.ids[key] = self.add(kind, state)
        aq.edit(self.live, self.ids["stale"], "op", "edited after placement")  # revision 0 placed, queue now at 1
        path = self.live / "approvals" / mail_drafts.JOURNAL
        path.write_text("".join(json.dumps({"schema": mail_drafts.SCHEMA, "at": "2026-11-10T15:00:00Z", "message_id": MSGID,
                                            "folder": FOLDER, **row}) + "\n" for row in self.journal))

    def add(self, kind, state):
        self.n += 1
        row = aq.add_draft(self.live, item_id="%064x" % (100 + self.n), kind=kind, agency_id="caltrans",
                           request_id="req-%d" % self.n, body="%s %d" % (BODY, self.n), evidence={"source": "test"},
                           source_run_id=RUN)
        if state:
            self.journal.append({"queue_id": row["id"], "revision": row["revision"], "state": state})
        return row["id"]

    def run_cli(self, name, key, revision, *extra, ident=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = gfj.main(["--export-root", str(self.root), "--name", name, "--queue-id", ident or self.ids[key],
                             "--revision", str(revision), *extra])
        return code, out.getvalue()

    def refused(self, name, key, revision, expect, status=1, **kw):
        code, text = self.run_cli(name, key, revision, **kw)
        self.assertEqual((code, text.split()[0]), (status, expect), text)
        return text

    def manifest_digest(self, name):
        return hashlib.sha256((self.root / "exports" / name / "EXPORT.json").read_bytes()).hexdigest()

    def set_time(self, dest, delta):
        manifest = json.loads((dest / "EXPORT.json").read_text())
        manifest["exported_at"] = stamp(delta)
        (dest / "EXPORT.json").write_text(json.dumps(manifest))


class Pass(Join):
    def test_c1_c2_exact_followup_row_placed_passes_with_the_manifest_digest(self):
        # Protects C1/C2. Bug: a matching id alone passes, a kind is dropped (engine, notice), a found_existing
        # placement is refused, or the printed digest is not the manifest's own SHA-256.
        name = self.make_export()
        for key, kind, state in FOLLOWUPS:
            with self.subTest(key):
                code, text = self.run_cli(name, key, 0, "--json")
                found = json.loads(text)
                self.assertEqual((code, found["code"], found["kind"], found["state"]), (0, None, kind, state))
                self.assertEqual(found["manifest_sha256"], self.manifest_digest(name))
        code, text = self.run_cli(name, "reminder", 0)
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("PASS ") and self.manifest_digest(name) in text, text)


class Refusals(Join):
    def test_c3_followup_row_missing(self):
        # Protects C3. Bug: an unknown id is reported as an unplaced or mismatched follow-up.
        self.refused(self.make_export(), None, 0, "FOLLOWUP_ROW_MISSING", ident="f" * 64)

    def test_c3_a_reply_draft_never_passes_as_a_followup(self):
        # Protects C3. Bug: any placed mail draft, or a signed placed reply (the real approved one included), is
        # accepted as the no-response follow-up because only "placed" and the id are checked.
        name = self.make_export()
        for key, revision, kind in (("reply", 1, "reply-extension"), ("appeal", 0, "appeal-denial"), ("fee", 0, "fee-reply")):
            with self.subTest(key):
                self.assertIn("kind=" + kind, self.refused(name, key, revision, "FOLLOWUP_NOT_FOLLOWUP_KIND"))
        self.refused(name, "reply_unplaced", 0, "FOLLOWUP_NOT_FOLLOWUP_KIND")

    def test_c3_not_followup_kind_for_first_request_missing_records_and_non_mail_decision(self):
        # Protects C3. Bug: a placed first request, a missing-records follow-up (not a no-response clock draft) or a
        # review copy counts as the no-response follow-up.
        name = self.make_export()
        for key in ("first", "missing", "decision"):
            with self.subTest(key):
                self.refused(name, key, 0, "FOLLOWUP_NOT_FOLLOWUP_KIND")

    def test_c3_not_placed_for_unplaced_and_failed_only_rows(self):
        # Protects C3. Bug: an approved or failed-only follow-up is treated as sitting in the Drafts mailbox.
        name = self.make_export()
        self.refused(name, "unplaced", 0, "FOLLOWUP_NOT_PLACED")
        self.assertIn("state=failed", self.refused(name, "failed", 0, "FOLLOWUP_NOT_PLACED"))

    def test_c3_revision_mismatch_for_superseded_and_wrong_revision(self):
        # Protects C3. Bug: a draft placed for an earlier revision, or any revision of the id, passes for the newer one.
        name = self.make_export()
        self.refused(name, "stale", 1, "FOLLOWUP_REVISION_MISMATCH")  # queue is at 1, only revision 0 was placed
        self.refused(name, "stale", 0, "FOLLOWUP_REVISION_MISMATCH")  # revision 0 is no longer a queue row
        self.refused(name, "reminder", 7, "FOLLOWUP_REVISION_MISMATCH")

    def test_c3_export_stale_after_max_age(self):
        # Protects C2/C3. Bug: age is ignored, or the default is not 36 hours.
        name = self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(hours=-37)))
        self.refused(name, "reminder", 0, "EXPORT_STALE")
        self.assertEqual(self.run_cli(name, "reminder", 0, "--max-age-hours", "48")[0], 0)
        fresh = self.make_export(tcs.RUN2, mutate=lambda dest: self.set_time(dest, timedelta(hours=-35)))
        self.assertEqual(self.run_cli(fresh, "reminder", 0)[0], 0)

    def test_c3_export_future_dated(self):
        # Protects C2/C3. Bug: a clock-skewed or forged future time counts as fresh.
        name = self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(hours=1)))
        self.refused(name, "reminder", 0, "EXPORT_FUTURE")

    def test_c3_unverified_export_exits_2(self):
        # Protects C3. Bug: a tampered, missing or incomplete export is read anyway or reported as a row refusal.
        name = self.make_export()
        self.refused("20260101T000000Z-" + RUN, "reminder", 0, "EXPORT_UNVERIFIED", status=2)  # not installed
        path = self.root / "exports" / name / "console-model" / "mail-drafts.json"
        path.chmod(0o600)
        path.write_text(path.read_text() + " ")  # sealed file edited after install: hash mismatch
        self.assertIn("export_hash_mismatch", self.refused(name, "reminder", 0, "EXPORT_UNVERIFIED", status=2))
        self.refused(self.make_export(tcs.RUN2, mutate=self.drop_queue), "reminder", 0, "EXPORT_UNVERIFIED", status=2)

    def test_c3_c4_exported_unavailable_code_is_never_echoed(self):
        # Protects C3/C4. Bug: a hash-valid export whose model says state=unavailable with its own "code" text makes the
        # EXPORT_UNVERIFIED reason print that text (draft text or an address) instead of a fixed reason.
        def hostile(dest):
            (dest / "console-model" / "queue.json").write_text(json.dumps({
                "schema": ce.MODEL_SCHEMA, "state": "unavailable", "source_project_id": self.pid,
                "code": "SENTINEL-CODE person@example.invalid"}))
        text = self.refused(self.make_export(mutate=hostile), "reminder", 0, "EXPORT_UNVERIFIED", status=2)
        self.assertEqual(text.strip(), "EXPORT_UNVERIFIED reason=console_model_unavailable")


class Binding(Join):
    def test_c1_swapped_export_changes_the_digest(self):
        # Protects C1. Bug: the digest names the export or the run instead of the manifest bytes read for this result.
        first, second = self.make_export(), self.make_export(tcs.RUN2)
        seen = [json.loads(self.run_cli(n, "reminder", 0, "--json")[1])["manifest_sha256"] for n in (first, second)]
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(seen, [self.manifest_digest(first), self.manifest_digest(second)])

    def test_c1_manifest_swapped_between_verification_and_result_is_refused(self):
        # Protects C1. Bug: freshness and signoff may cite different manifests (manifest bytes not re-read and compared).
        name = self.make_export()
        real, calls = gfj._manifest, []

        def swap(pin):
            calls.append(1)
            raw, manifest = real(pin)
            return (raw, manifest) if len(calls) == 1 else (raw + b" ", manifest)
        with mock.patch.object(gfj, "_manifest", swap):
            self.assertIn("export_changed", self.refused(name, "reminder", 0, "EXPORT_UNVERIFIED", status=2))

    def test_c1_queue_or_drafts_swapped_only_while_read_is_refused(self):
        # Protects C1. Bug: the pin is checked before and after the read, so a file swapped for the read and restored
        # afterwards is joined unseen (a placed reply read as a reminder passed with the old digest).
        name = self.make_export()
        model = self.root / "exports" / name / "console-model"
        for fname, method, old, new in (("queue.json", "queue_rows", b'"first-request"', b'"reminder-no-response"'),
                                        ("mail-drafts.json", "mail_drafts", b'"placed"', b'"found_existing"')):
            path, real = model / fname, getattr(gfj.Bound, method)
            original = path.read_bytes()
            self.assertIn(old, original)

            def swap(source, _real=real, _path=path, _original=original, _old=old, _new=new):
                _path.chmod(0o600)
                _path.write_bytes(_original.replace(_old, _new))
                try:
                    return _real(source)
                finally:
                    _path.write_bytes(_original)
            with mock.patch.object(gfj.Bound, method, swap):
                self.assertIn("export_hash_mismatch", self.refused(name, "first", 0, "EXPORT_UNVERIFIED", status=2))
            self.assertEqual(path.read_bytes(), original)
            self.refused(name, "first", 0, "FOLLOWUP_NOT_FOLLOWUP_KIND")  # untouched export keeps its honest refusal


class NoEcho(Join):
    def test_c4_output_has_only_ids_kind_state_and_digests_and_changes_nothing(self):
        # Protects C4. Bug: the row or journal entry is echoed (draft text, folder, message id), or the mailbox is touched.
        name = self.make_export()
        before = (tree_state(self.root), tree_state(self.live))
        with mock.patch("imaplib.IMAP4_SSL", side_effect=AssertionError("mailbox opened")), \
                mock.patch("imaplib.IMAP4", side_effect=AssertionError("mailbox opened")):
            runs = [self.run_cli(name, k, r, *extra) for k, r in (("reminder", 0), ("engine", 0), ("first", 0), ("failed", 0),
                                                                 ("stale", 1))
                    for extra in ((), ("--json",))]
        self.assertEqual((tree_state(self.root), tree_state(self.live)), before)
        for code, text in runs:
            for secret in (BODY, FOLDER, MSGID, "@", "example.invalid", "caltrans", "req-"):
                self.assertNotIn(secret, text)
            if text.startswith("{"):
                self.assertLessEqual(set(json.loads(text)), OUT_KEYS)


if __name__ == "__main__":
    import unittest
    unittest.main()
