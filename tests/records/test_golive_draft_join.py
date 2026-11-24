"""golive_draft_join (DFS1123-2312-13): one (queue id, revision) is a real reply draft in ONE pinned export.

Fixtures are synthetic: a private temp project and root built by the repo's own export writer (Panel.make_export), so
the join reads exactly what a real sealed export holds. Numbered contracts C1-C4 are the ones in the card."""
import hashlib
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from campaign_tool.records import approval_queue as aq, golive_draft_join as gdj, mail_drafts
from tests.records import test_console_snapshot as tcs
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN

BODY, FOLDER, MSGID = "SENTINEL-BODY-TEXT", "SENTINEL-FOLDER", "<sentinel-id@example.invalid>"
OUT_KEYS = {"code", "export", "exported_at", "kind", "manifest_sha256", "queue_id", "revision", "run_id", "state"}


def stamp(delta):
    return (datetime.now(timezone.utc) + delta).strftime(gdj.STAMP)


class Join(tcs.Panel):
    def setUp(self):
        super().setUp()
        self.n, self.journal = 0, [{"queue_id": self.approved_id, "revision": 1, "state": "placed"}]
        self.ids = {"ok": self.approved_id, "unplaced": self.pending_id}  # signed reply-extension rev 1 placed / rev 0 none
        for key, kind, state in (("appeal", "appeal-denial", "found_existing"), ("fee", "fee-reply", "placed"),
                                 ("failed", "reply-extension", "failed"), ("first", "first-request", "placed"),
                                 ("followup", "followup-missing-records", "placed"),
                                 ("decision", "draft-agency-summary", "placed"), ("stale", "reply-extension", "placed")):
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
        self.journal.append({"queue_id": row["id"], "revision": row["revision"], "state": state})
        return row["id"]

    def run_cli(self, name, key, revision, *extra, ident=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = gdj.main(["--export-root", str(self.root), "--name", name, "--queue-id", ident or self.ids[key],
                             "--revision", str(revision), *extra])
        return code, out.getvalue()

    def refused(self, name, key, revision, expect, status=1, **kw):
        code, text = self.run_cli(name, key, revision, **kw)
        self.assertEqual((code, text.split()[0]), (status, expect), text)
        return text

    def manifest_digest(self, name):
        return hashlib.sha256((self.root / "exports" / name / "EXPORT.json").read_bytes()).hexdigest()


class Pass(Join):
    def test_c1_c2_exact_reply_row_placed_passes_with_the_manifest_digest(self):
        # Protects C1/C2. Bug: a nonzero placement count or a matching id alone passes, or the digest is not the manifest's.
        name = self.make_export()
        for key, revision, kind, state in (("ok", 1, "reply-extension", "placed"), ("appeal", 0, "appeal-denial", "found_existing"),
                                           ("fee", 0, "fee-reply", "placed")):
            with self.subTest(key):
                code, text = self.run_cli(name, key, revision, "--json")
                found = json.loads(text)
                self.assertEqual((code, found["code"], found["kind"], found["state"]), (0, None, kind, state))
                self.assertEqual(found["manifest_sha256"], self.manifest_digest(name))
        code, text = self.run_cli(name, "ok", 1)
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("PASS ") and self.manifest_digest(name) in text, text)


class Refusals(Join):
    def test_c3_reply_row_missing(self):
        # Protects C3. Bug: an unknown id is reported as an unplaced or mismatched reply.
        self.refused(self.make_export(), None, 0, "REPLY_ROW_MISSING", ident="f" * 64)

    def test_c3_not_reply_kind_for_first_request_followup_and_non_mail_decision(self):
        # Protects C3. Bug: a placed first request, follow-up or review copy counts as the reply draft.
        name = self.make_export()
        for key in ("first", "followup", "decision"):
            with self.subTest(key):
                self.refused(name, key, 0, "REPLY_NOT_REPLY_KIND")

    def test_c3_not_placed_for_unplaced_and_failed_only_rows(self):
        # Protects C3. Bug: an approved or failed-only reply is treated as sitting in the Drafts mailbox.
        name = self.make_export()
        self.refused(name, "unplaced", 0, "REPLY_NOT_PLACED")
        self.refused(name, "failed", 0, "REPLY_NOT_PLACED")

    def test_c3_revision_mismatch_for_superseded_and_wrong_revision(self):
        # Protects C3. Bug: a draft placed for an earlier revision, or any revision of the id, passes for the newer one.
        name = self.make_export()
        self.refused(name, "stale", 1, "REPLY_REVISION_MISMATCH")  # queue is at 1, only revision 0 was placed
        self.refused(name, "stale", 0, "REPLY_REVISION_MISMATCH")  # revision 0 is no longer a queue row
        self.refused(name, "ok", 7, "REPLY_REVISION_MISMATCH")

    def test_c3_export_stale_after_max_age(self):
        # Protects C2/C3. Bug: age is ignored, or the default is not 36 hours.
        name = self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(hours=-37)))
        self.refused(name, "ok", 1, "EXPORT_STALE")
        self.assertEqual(self.run_cli(name, "ok", 1, "--max-age-hours", "48")[0], 0)
        fresh = self.make_export(tcs.RUN2, mutate=lambda dest: self.set_time(dest, timedelta(hours=-35)))
        self.assertEqual(self.run_cli(fresh, "ok", 1)[0], 0)

    def test_c3_export_future_dated(self):
        # Protects C2/C3. Bug: a clock-skewed or forged future time counts as fresh.
        self.refused(self.make_export(mutate=lambda dest: self.set_time(dest, timedelta(hours=1))), "ok", 1, "EXPORT_FUTURE")

    def test_c3_unverified_export_exits_2(self):
        # Protects C3. Bug: a tampered, missing or incomplete export is read anyway or reported as a row refusal.
        name = self.make_export()
        self.refused("20260101T000000Z-" + RUN, "ok", 1, "EXPORT_UNVERIFIED", status=2)  # not installed
        path = self.root / "exports" / name / "console-model" / "mail-drafts.json"
        path.chmod(0o600)
        path.write_text(path.read_text() + " ")  # sealed file edited after install: hash mismatch
        self.assertIn("export_hash_mismatch", self.refused(name, "ok", 1, "EXPORT_UNVERIFIED", status=2))
        self.refused(self.make_export(tcs.RUN2, mutate=self.drop_queue), "ok", 1, "EXPORT_UNVERIFIED", status=2)

    def set_time(self, dest, delta):
        manifest = json.loads((dest / "EXPORT.json").read_text())
        manifest["exported_at"] = stamp(delta)
        (dest / "EXPORT.json").write_text(json.dumps(manifest))


class Binding(Join):
    def test_c1_swapped_export_changes_the_digest(self):
        # Protects C1. Bug: the digest names the export or the run instead of the manifest bytes read for this result.
        first, second = self.make_export(), self.make_export(tcs.RUN2)
        seen = [json.loads(self.run_cli(n, "ok", 1, "--json")[1])["manifest_sha256"] for n in (first, second)]
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(seen, [self.manifest_digest(first), self.manifest_digest(second)])

    def test_c1_manifest_swapped_between_verification_and_result_is_refused(self):
        # Protects C1. Bug: freshness and signoff may cite different manifests (manifest bytes not re-read and compared).
        name = self.make_export()
        real = gdj._manifest_bytes
        calls = []

        def swap(pin):
            calls.append(1)
            return real(pin) if len(calls) == 1 else real(pin) + b" "
        with mock.patch.object(gdj, "_manifest_bytes", swap):
            self.assertIn("export_changed", self.refused(name, "ok", 1, "EXPORT_UNVERIFIED", status=2))


class NoEcho(Join):
    def test_c4_output_has_only_ids_kind_state_and_digests_and_changes_nothing(self):
        # Protects C4. Bug: the row or journal entry is echoed (draft text, folder, message id), or the mailbox is touched.
        name = self.make_export()
        before = (tree_state(self.root), tree_state(self.live))
        with mock.patch("imaplib.IMAP4_SSL", side_effect=AssertionError("mailbox opened")), \
                mock.patch("imaplib.IMAP4", side_effect=AssertionError("mailbox opened")):
            runs = [self.run_cli(name, k, r, *extra) for k, r in (("ok", 1), ("first", 0), ("failed", 0), ("stale", 1))
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
