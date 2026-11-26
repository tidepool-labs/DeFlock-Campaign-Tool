"""Shared reply target at the exact-revision placement boundary (D48).

``reply_placement_preview`` refuses ``target_shared`` when another approved, not yet placed mail row answers the
same agency message. A second approval can land after that preview succeeds, so the exact command
``mail_drafts.place(only_id=..., only_revision=...)`` re-checks the same condition under the queue lock and refuses
``exact_target_shared`` before connecting or writing anything. Bulk placement is unchanged. Fake IMAP only.
"""
import contextlib
import hashlib
import io
import json

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import mail_drafts as md
from campaign_tool.records import mail_poll
from campaign_tool.records import reply_placement_preview as rpp
from campaign_tool.records.outcomes import received_items
from tests.records.test_mail_drafts import CANARY, DOC, EML, EVIDENCE, NOW, Base, FakeIMAP
from unittest import mock

PLACED_ONE = {"placed": 1, "existing": 0, "failed": 0, "skipped": 0}


class SharedTargetTests(Base):
    def add(self, kind, item=None, approve=True):
        row = aq.add_draft(self.project, item_id=item or received_items.item_id(DOC), kind=kind,
                           agency_id="agency-1", request_id="req-1", body=f"Reply text. {CANARY}\n",
                           evidence=EVIDENCE, source_run_id="run-1")
        return self.approve(row["id"]) if approve else row

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
        self.assertEqual(self.fake.messages, [])

    def queue_row(self, ident):
        return next(r for r in aq.load_queue(self.project) if r["id"] == ident)

    def test_competing_approval_after_a_successful_preview_refuses_with_zero_connects(self):
        # Contract: the exact command repeats the preview's shared-target check. Bug caught (D42 test 2): a second
        # approval for the same agency message added after WOULD_PLACE let place(only_id, only_revision) append.
        self.build_ledger()
        a = self.add("reply-extension")
        self.assertEqual(rpp.preview(self.project, a["id"], a["revision"], config=self.config)["verdict"],
                         rpp.WOULD_PLACE)
        b = self.add("fee-reply")
        self.assertIn("target_shared", rpp.preview(self.project, a["id"], a["revision"])["refusals"])
        neighbour_before = self.queue_row(b["id"])
        self.refused("exact_target_shared", only_id=a["id"], only_revision=a["revision"])
        self.refused("exact_target_shared", only_id=a["id"], only_revision=a["revision"], dry_run=True)
        self.refused("exact_target_shared", only_id=b["id"], only_revision=b["revision"])
        self.assertEqual(self.queue_row(b["id"]), neighbour_before)
        self.assertFalse(self.journal_path().exists())

    def test_replies_to_different_parts_of_one_message_count_as_one_target(self):
        # The document reply and the reply to the message itself resolve to the same agency Message-ID.
        self.build_ledger()
        a = self.add("reply-extension")
        other = self.add("reply-extension", item=received_items.item_id(EML))
        self.assertNotEqual(a["item_id"], other["item_id"])
        self.refused("exact_target_shared", only_id=a["id"], only_revision=a["revision"])

    def test_after_the_competitor_is_withdrawn_the_unchanged_exact_target_places_alone(self):
        self.build_ledger()
        a = self.add("reply-extension")
        b = self.add("fee-reply")
        self.refused("exact_target_shared", only_id=a["id"], only_revision=a["revision"])
        withdrawn = aq.edit(self.project, b["id"], "organizer", "Withdrawn for now.\n")
        self.assertEqual(withdrawn["status"], "pending")
        neighbour_before = self.queue_row(b["id"])
        self.assertEqual(self.place(only_id=a["id"], only_revision=a["revision"]), PLACED_ONE)
        (row,) = md.placements(self.project)
        self.assertEqual((row["queue_id"], row["revision"], row["state"]), (a["id"], a["revision"], "placed"))
        self.assertEqual(self.fake.calls.count("append"), 1)
        self.assertEqual(len(self.fake.messages), 1)
        self.assertEqual(self.queue_row(b["id"]), neighbour_before)

    def test_unrelated_or_unresolved_approved_rows_do_not_block(self):
        self.build_ledger()
        a = self.add("reply-extension")
        unresolved = self.add("fee-reply", item="1" * 64)  # no stored agency message: answers nothing here
        review_copy = aq.add_draft(self.project, item_id=received_items.item_id(DOC), kind="draft-agency-summary",
                                   agency_id="agency-1", request_id="req-1", body="Summary for review.\n",
                                   evidence={}, source_run_id="r1")
        self.assertEqual(self.place(only_id=a["id"], only_revision=a["revision"]), PLACED_ONE)
        self.assertEqual({p["queue_id"] for p in md.placements(self.project)}, {a["id"]})
        self.assertNotIn(unresolved["id"], {p["queue_id"] for p in md.placements(self.project)})
        self.assertNotIn(review_copy["id"], {p["queue_id"] for p in md.placements(self.project)})

    def test_a_competitor_already_placed_is_not_a_competitor_like_the_preview(self):
        self.build_ledger()
        b = self.add("fee-reply")
        a = self.add("reply-extension", approve=False)
        self.assertEqual(self.place(only_id=b["id"], only_revision=b["revision"]), PLACED_ONE)
        a = self.approve(a["id"])
        self.assertNotIn("target_shared", rpp.preview(self.project, a["id"], a["revision"])["refusals"])
        self.assertEqual(self.place(only_id=a["id"], only_revision=a["revision"]), PLACED_ONE)
        self.assertEqual(len(self.fake.messages), 2)

    def test_a_competing_approval_cannot_land_between_the_check_and_the_append(self):
        # The queue lock taken for the check is held through the append, so a new approval waits and fails.
        self.build_ledger()
        a = self.add("reply-extension")
        b = self.add("fee-reply", approve=False)
        payload = aq.approval_payload(self.project, b["id"])
        signature = self.signer.sign(payload)
        attempts = []

        class Racing(FakeIMAP):
            def append(inner, folder, flags, date, raw):
                with mock.patch.object(aq, "LOCK_SECONDS", 0.05):
                    with self.assertRaises(aq.ApprovalQueueError) as caught:
                        aq.approve(self.project, b["id"], {"kind": "ssh", "signature": signature})
                    attempts.append(caught.exception.code)
                return super().append(folder, flags, date, raw)

        racing = Racing()
        self.assertEqual(md.place(self.project, self.config, factory=racing.factory, now=NOW, only_id=a["id"],
                                  only_revision=a["revision"]), PLACED_ONE)
        self.assertEqual(attempts, ["queue_busy"])
        self.assertEqual(self.queue_row(b["id"])["status"], "pending")

    def test_bulk_placement_is_unchanged(self):
        self.build_ledger()
        self.add("reply-extension")
        self.add("fee-reply")
        summary = self.place()
        self.assertEqual(summary["placed"] + summary["failed"], 2)


class SharedTargetCommandLineTests(Base):
    def test_exact_cli_refusal_is_code_only(self):
        self.build_ledger()
        rows = []
        for kind in ("reply-extension", "fee-reply"):
            row = aq.add_draft(self.project, item_id=received_items.item_id(DOC), kind=kind, agency_id="agency-1",
                               request_id="req-1", body=f"Reply text. {CANARY}\n", evidence=EVIDENCE,
                               source_run_id="run-1")
            rows.append(self.approve(row["id"]))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mail_poll, "connect", side_effect=AssertionError("connect")):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = md.main(["place", "--project", str(self.project), "--config", str(self.config_path),
                                "--queue-id", rows[0]["id"], "--revision", str(rows[0]["revision"])])
        self.assertEqual((code, out.getvalue(), err.getvalue()), (1, "", "exact_target_shared\n"))
        self.assertNotIn(CANARY, err.getvalue())
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(json.loads(json.dumps(md.placements(self.project))), [])
