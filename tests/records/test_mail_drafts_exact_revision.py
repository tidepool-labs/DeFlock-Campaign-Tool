"""Exact-revision Drafts placement (D31): place one reviewed (queue ID, revision) or nothing at all.

The reviewer previews one exact revision; an edit, a new approval or a cancellation before placement moves the row
to another revision. ``mail_drafts.place(only_id=..., only_revision=...)`` and ``records mail-drafts place
--queue-id ID --revision N`` must then refuse before writing anything or connecting. Fake IMAP only.
"""
import contextlib
import hashlib
import io
import json

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import mail_drafts as md
from campaign_tool.records import mail_poll
from tests.records.test_mail_drafts import CANARY, NOW, USER, Base, FakeIMAP
from unittest import mock

PLACED_ONE = {"placed": 1, "existing": 0, "failed": 0, "skipped": 0}


class ExactRevisionTests(Base):
    def tree(self):
        """Every path, mode and byte under the project, except SQLite's own shared-memory/WAL side files."""
        out = {}
        for path in sorted(self.project.rglob("*")):
            if path.name.endswith(("-shm", "-wal")):
                continue
            info = path.lstat()
            out[str(path)] = (info.st_mode, hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        return out

    def refused(self, code, **kw):
        before = self.tree()
        with mock.patch.object(mail_poll, "connect", side_effect=AssertionError("connect")):
            with self.assertRaises(md.MailDraftError) as caught:
                self.place(**kw)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.journal_path().exists())
        self.assertFalse((self.project / "approvals" / md.LOCK).exists())

    def test_reviewed_revision_places_only_that_row(self):
        self.build_ledger()
        reply = self.draft()
        neighbour = self.draft(item="1" * 64)  # approved too; the bulk command would place it
        self.assertEqual(self.place(only_id=reply["id"], only_revision=reply["revision"]), PLACED_ONE)
        (row,) = md.placements(self.project)
        self.assertEqual((row["queue_id"], row["revision"], row["state"]), (reply["id"], reply["revision"], "placed"))
        self.assertEqual(row["message_id"], md.message_id(reply["id"], reply["revision"], USER))
        self.assertEqual(len(self.fake.messages), 1)
        self.assertNotIn(neighbour["id"], {p["queue_id"] for p in md.placements(self.project)})
        # running it again is still exactly one draft
        self.assertEqual(self.place(only_id=reply["id"], only_revision=reply["revision"]),
                         {"placed": 0, "existing": 0, "failed": 0, "skipped": 1})
        self.assertEqual(len(self.fake.messages), 1)

    def test_edit_after_review_refuses_whether_or_not_approved_again(self):
        self.build_ledger()
        reply = self.draft()
        reviewed = reply["revision"]
        aq.edit(self.project, reply["id"], "organizer", f"Edited after the review. {CANARY}\n")
        self.refused("exact_revision_not_current", only_id=reply["id"], only_revision=reviewed)
        approved = self.approve(reply["id"])
        self.assertEqual(approved["status"], "approved")
        self.refused("exact_revision_not_current", only_id=reply["id"], only_revision=reviewed)
        # without the exact revision the existing call places the newer approved text: that is what this guards
        self.assertEqual(self.place(only_id=reply["id"]), PLACED_ONE)
        (row,) = md.placements(self.project)
        self.assertEqual(row["revision"], approved["revision"])
        self.assertNotEqual(row["revision"], reviewed)

    def test_pending_or_cancelled_current_revision_refuses(self):
        self.build_ledger()
        pending = self.draft(approve=False)
        self.refused("exact_not_approved", only_id=pending["id"], only_revision=pending["revision"])
        reply = self.draft(item="2" * 64)
        aq.edit(self.project, reply["id"], "organizer", "Withdrawn.\n")
        cancelled = aq.reject(self.project, reply["id"], "organizer", "cancelled")
        self.refused("exact_not_approved", only_id=reply["id"], only_revision=cancelled["revision"])
        self.refused("exact_revision_not_current", only_id=reply["id"], only_revision=reply["revision"])

    def test_inexact_unknown_and_non_mail_requests_refuse(self):
        self.build_ledger()
        reply = self.draft()
        for only_id, revision, code in ((reply["id"][:12], 1, "exact_request_invalid"),
                                        (reply["id"].upper(), 1, "exact_request_invalid"),
                                        (None, 1, "exact_request_invalid"),
                                        (reply["id"], -1, "exact_request_invalid"),
                                        (reply["id"], True, "exact_request_invalid"),
                                        (reply["id"], "1", "exact_request_invalid"),
                                        ("f" * 64, 0, "exact_id_unknown")):
            with self.subTest(only_id=only_id, revision=revision):
                self.refused(code, only_id=only_id, only_revision=revision)
        review_copy = aq.add_draft(self.project, item_id="route:synthetic", kind="draft-agency-summary",
                                   agency_id="alpha-pd", request_id="OLD-1", body="Summary for review.\n",
                                   evidence={}, source_run_id="r1")
        self.refused("exact_not_mail", only_id=review_copy["id"], only_revision=review_copy["revision"])

    def test_dry_run_checks_the_same_exact_revision_and_writes_nothing(self):
        self.build_ledger()
        reply = self.draft()
        before = self.tree()
        self.assertEqual(self.place(only_id=reply["id"], only_revision=reply["revision"], dry_run=True)["would_place"],
                         1)
        self.assertEqual(self.tree(), before)
        aq.edit(self.project, reply["id"], "organizer", "Edited.\n")
        self.refused("exact_revision_not_current", only_id=reply["id"], only_revision=reply["revision"],
                     dry_run=True)

    def test_edit_or_cancel_cannot_complete_while_the_reviewed_revision_is_placed(self):
        # Contract: validation and the Drafts append see one queue state. Bug caught: the queue lock was released
        # after validation, so an edit/cancel completing before the append still left the stale revision in Drafts.
        self.build_ledger()
        reply = self.draft()
        attempts = []

        class Racing(FakeIMAP):
            def append(inner, folder, flags, date, raw):
                with mock.patch.object(aq, "LOCK_SECONDS", 0.05):
                    for name, call in (("edit", lambda: aq.edit(self.project, reply["id"], "organizer", "Late.\n")),
                                       ("reject", lambda: aq.reject(self.project, reply["id"], "organizer", "late"))):
                        with self.assertRaises(aq.ApprovalQueueError) as caught:
                            call()
                        attempts.append((name, caught.exception.code))
                return super().append(folder, flags, date, raw)

        racing = Racing()
        self.assertEqual(md.place(self.project, self.config, factory=racing.factory, now=NOW, only_id=reply["id"],
                                  only_revision=reply["revision"]), PLACED_ONE)
        self.assertEqual(attempts, [("edit", "queue_busy"), ("reject", "queue_busy")])
        (row,) = md.placements(self.project)
        self.assertEqual(row["revision"], reply["revision"])
        self.assertEqual([r["revision"] for r in aq.load_queue(self.project) if r["id"] == reply["id"]],
                         [reply["revision"]])
        # the lock is released afterwards: the edit now goes through (and is a new revision, never placed)
        self.assertGreater(aq.edit(self.project, reply["id"], "organizer", "Late.\n")["revision"], reply["revision"])

    def test_bulk_placement_is_unchanged(self):
        self.build_ledger()
        self.draft()
        self.draft(item="1" * 64)
        summary = self.place()
        self.assertEqual(summary["placed"] + summary["failed"], 2)


class ExactRevisionCommandLineTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = md.main(["place", "--project", str(self.project), "--config", str(self.config_path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_queue_id_and_revision_only_together_and_refusals_are_codes(self):
        self.build_ledger()
        reply = self.draft()
        blocked = mock.patch.object(mail_poll, "connect", side_effect=AssertionError("connect"))
        with blocked:
            for args in (("--queue-id", reply["id"]), ("--revision", str(reply["revision"]))):
                with self.subTest(args=args):
                    self.assertEqual(self.run_cli(*args), (1, "", "exact_request_invalid\n"))
            aq.edit(self.project, reply["id"], "organizer", "Edited.\n")
            self.assertEqual(self.run_cli("--queue-id", reply["id"], "--revision", str(reply["revision"])),
                             (1, "", "exact_revision_not_current\n"))
            code, out, _ = self.run_cli("--queue-id", reply["id"], "--revision", str(reply["revision"] + 1),
                                        "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertFalse(self.journal_path().exists())

    def test_malformed_revision_is_a_code_only_refusal_that_never_echoes_the_value(self):
        # Contract: a bad --revision is exact_request_invalid, exit 1, code-only stderr. Bug caught: argparse type=int
        # exited 2 and printed the rejected text (possibly draft text) to stderr; a leading-dash value hit the same.
        self.build_ledger()
        reply = self.draft()
        blocked = mock.patch.object(mail_poll, "connect", side_effect=AssertionError("connect"))
        with blocked:
            for bad in (CANARY, "-" + CANARY, "abc", "-1", "+1", "1.5", " 1", "1 ", "\uff11", "", "1" * 10, "0x1"):
                with self.subTest(revision=bad):
                    code, out, err = self.run_cli("--queue-id", reply["id"], "--revision", bad)
                    self.assertEqual((code, out, err), (1, "", "exact_request_invalid\n"))
        self.assertFalse(self.journal_path().exists())

    def test_missing_repeated_and_abbreviated_revision_forms_are_code_only_refusals(self):
        # Contract: every --revision occurrence is checked before argparse, so a bare trailing option, a repeated
        # option (any order, any spelling, valid or not) or an abbreviation of it is exact_request_invalid, exit 1,
        # code-only stderr (never argparse usage/exit 2, never a silent last-value-wins). Nothing connects or is written.
        self.build_ledger()
        reply = self.draft()
        good = str(reply["revision"])
        forms = (
            ("--queue-id", reply["id"], "--revision"),
            ("--revision", "--queue-id", reply["id"]),
            ("--queue-id", reply["id"], "--revision", good, "--revision", "-" + CANARY),
            ("--queue-id", reply["id"], "--revision=" + CANARY, "--revision=" + good),
            ("--queue-id", reply["id"], "--revision=" + good, "--revision=" + CANARY),
            ("--queue-id", reply["id"], "--revision=" + good, "--revision", good),
            ("--queue-id", reply["id"], "--revision=" + good, "--revision=" + good),
            ("--queue-id", reply["id"], "--rev", CANARY),
            ("--queue-id", reply["id"], "--rev=" + CANARY),
            ("--queue-id", reply["id"], "--revision=" + CANARY),
            ("--queue-id", reply["id"], "--revision", "--dry-run"),
        )
        blocked = mock.patch.object(mail_poll, "connect", side_effect=AssertionError("connect"))
        with blocked:
            for args in forms:
                with self.subTest(args=args):
                    code, out, err = self.run_cli(*args)
                    self.assertEqual((code, out, err), (1, "", "exact_request_invalid\n"))
        self.assertFalse(self.journal_path().exists())

    def test_one_well_formed_revision_in_any_accepted_spelling_still_goes_through_to_the_exact_check(self):
        # Guards the refusal above against being over-broad: "--revision N", "--revision=N" and "--rev N" all reach the
        # exact-revision check (here a dry run), and exactly one is accepted.
        self.build_ledger()
        reply = self.draft()
        blocked = mock.patch.object(mail_poll, "connect", side_effect=AssertionError("connect"))
        with blocked:
            for spelling in (("--revision", str(reply["revision"])), ("--revision=%d" % reply["revision"],),
                             ("--rev", str(reply["revision"]))):
                with self.subTest(spelling=spelling):
                    code, out, err = self.run_cli("--queue-id", reply["id"], *spelling, "--dry-run")
                    self.assertEqual((code, err), (0, ""), err)
                    self.assertNotIn(CANARY, out)

    def test_exact_cli_places_one_row_through_the_fake_mailbox(self):
        self.build_ledger()
        reply = self.draft()
        other = self.draft(item="1" * 64)
        fake = FakeIMAP()
        with mock.patch.object(md.place, "__kwdefaults__", dict(md.place.__kwdefaults__ or {}, factory=fake.factory)):
            code, out, err = self.run_cli("--queue-id", reply["id"], "--revision", str(reply["revision"]))
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(json.loads(out), PLACED_ONE)
        self.assertNotIn(CANARY, out)
        self.assertEqual({p["queue_id"] for p in md.placements(self.project)}, {reply["id"]})
        self.assertNotIn(other["id"], {p["queue_id"] for p in md.placements(self.project)})
        self.assertLessEqual(set(fake.calls), {"select", "search", "append", "logout"})
