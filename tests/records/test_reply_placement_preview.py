"""D22: one-reply Drafts placement preview on copied synthetic state. Offline; FakeIMAP only in the placement check."""
import contextlib
import hashlib
import io
import json
import os
import smtplib
import socket
import tempfile
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import draft_templates
from campaign_tool.records import mail_drafts as md
from campaign_tool.records import mail_poll
from campaign_tool.records import reply_placement_preview as rpp
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import received_items
from tests.approval_signing import Signer

EML, DOC = "e" * 64, "d" * 64
USER = "records@campaign.example"
CANARY = "CANARY-PREVIEW-BODY-5521"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
AGENCY_MID = "<agency-1@agency.example>"


class FakeIMAP:
    """Allows exactly select, uid SEARCH, append and logout."""

    def __init__(self):
        self.messages, self.calls = [], []

    def __getattr__(self, name):
        raise AssertionError("unexpected IMAP call: " + name)

    def select(self, folder, readonly=False):
        self.calls.append("select")
        return "OK", [b"1"]

    def uid(self, command, *args):
        assert command == "SEARCH", command
        self.calls.append("search")
        wanted = args[-1].strip('"')
        hits = [str(i + 1).encode() for i, m in enumerate(self.messages) if wanted.encode() in m]
        return "OK", [b" ".join(hits)]

    def append(self, folder, flags, date, raw):
        self.calls.append("append")
        self.messages.append(raw)
        return "OK", [b"done"]

    def logout(self):
        self.calls.append("logout")
        return "BYE", []

    def factory(self, config, password):
        return self


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="placement-preview-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)
        private = self.root / "private"
        private.mkdir(mode=0o700)
        pw = private / "pw"
        pw.write_text("synthetic-secret\n")
        pw.chmod(0o600)
        config = private / "mail.json"
        config.write_text(json.dumps({"host": "imap.example.invalid", "port": 993, "user": USER,
                                      "password_file": str(pw), "folder": "Drafts"}))
        config.chmod(0o600)
        self.config_path = config
        self.config = md.load_config(config)
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)
        for tripwire in (mock.patch.object(smtplib, "SMTP", side_effect=AssertionError("smtp")),
                         mock.patch.object(socket, "create_connection", side_effect=AssertionError("socket")),
                         mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("http"))):
            tripwire.start()
            self.addCleanup(tripwire.stop)

    def build_ledger(self, headers=None, extra_docs=()):
        """One agency message (EML) with attached document DOC (plus ``extra_docs``); replies answer those items."""
        ledger = self.project / "processing-v1" / "ledger.sqlite"
        store.initialize(ledger)
        blob = self.root / "blobs" / EML
        blob.parent.mkdir(exist_ok=True, mode=0o700)
        blob.write_text(headers or ("From: Clerk <clerk@agency.example>\r\nReply-To: records-desk@agency.example\r\n"
                                    f"Subject: Your request 4471\r\nMessage-ID: {AGENCY_MID}\r\n\r\nbody\r\n"))
        with store.ledger(ledger) as con:
            for digest in (EML, DOC, *extra_docs):
                con.execute("INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,"
                            "preservation_status,provenance_json,storage_path) VALUES(?,?,?,?,?,?,?,?,?)",
                            (digest, 10, "x/y", "2026-11-02T18:59:00Z", "agency_original", "in_scope", "verified",
                             "{}", str(blob) if digest == EML else None))
            con.execute("INSERT INTO occurrences VALUES('o1',?,'mail','m',NULL,NULL,'t','{}')", (EML,))
            for index, digest in enumerate((DOC, *extra_docs)):
                con.execute("INSERT INTO occurrences VALUES(?,?,'attachment','p','o1',NULL,'t','{}')",
                            ("o%d" % (index + 2), digest))
            con.execute("INSERT INTO mail_messages VALUES('msg-1','acct','INBOX',1,1,?,?,?,?,'x')",
                        (AGENCY_MID, EML, "{}", "2026-11-02T18:59:00Z"))
            con.commit()

    def draft(self, item=None, kind="reply-extension", body=None, approve=True):
        row = aq.add_draft(self.project, item_id=item or received_items.item_id(DOC), kind=kind,
                           agency_id="agency-1", request_id="req-1", body=body or f"Please extend. {CANARY}\n",
                           evidence={"findings": [], "deadlines": []}, source_run_id="run-1")
        return self.approve(row["id"]) if approve else row

    def approve(self, ident):
        sig = self.signer.sign(aq.approval_payload(self.project, ident))
        return aq.approve(self.project, ident, {"kind": "ssh", "signature": sig})

    def preview(self, row, revision=None, **kw):
        kw.setdefault("config", self.config)
        return rpp.preview(self.project, row["id"], row["revision"] if revision is None else revision, **kw)

    def tree(self):
        """Every path, mode and byte under the project and the blob store, except SQLite's own shared-memory and
        write-ahead files, which the existing read-only ledger open (``mail_drafts.find_thread``) may create."""
        out = {}
        for base in (self.project, self.root / "blobs"):
            for path in sorted(base.rglob("*")) if base.exists() else []:
                if path.name in ("ledger.sqlite-shm", "ledger.sqlite-wal"):
                    continue
                info = path.lstat()
                out[str(path)] = (info.st_mode, hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        return out


class ExactReplyTests(Base):
    def test_exact_approved_reply_would_place_and_matches_the_real_exact_id_placement(self):
        self.build_ledger()
        reply = self.draft()
        neighbour = self.draft(item="1" * 64)  # another approved reply that a bulk placement would also place
        result = self.preview(reply)
        self.assertEqual((result["verdict"], result["refusals"]), (rpp.WOULD_PLACE, []))
        self.assertEqual((result["queue_id"], result["revision"], result["current_revision"]),
                         (reply["id"], 1, 1))
        self.assertEqual((result["threaded"], result["to_domain"], result["folder"]), (True, "agency.example", "Drafts"))
        self.assertEqual(result["message_id"], md.message_id(reply["id"], 1, USER))
        self.assertEqual(result["others_untouched"], [neighbour["id"]])
        self.assertEqual(result["bulk_place_would_place"], 2)
        self.assertNotIn(CANARY, json.dumps(result))
        # the separately authorized exact-ID placement produces exactly the previewed bytes and leaves the neighbour
        fake = FakeIMAP()
        self.assertEqual(md.place(self.project, self.config, factory=fake.factory, now=NOW, only_id=reply["id"]),
                         {"placed": 1, "existing": 0, "failed": 0, "skipped": 0})
        (placed,) = md.placements(self.project)
        self.assertEqual((placed["queue_id"], placed["revision"]), (reply["id"], 1))
        self.assertEqual((placed["fingerprint"], placed["message_id"]), (result["fingerprint"], result["message_id"]))
        again = self.preview(reply)
        self.assertEqual((again["verdict"], again["refusals"], again["journal_state"]),
                         (rpp.REFUSED, ["already_placed"], "placed"))
        self.assertEqual((again["others_untouched"], again["bulk_place_would_place"]), ([neighbour["id"]], 1))
        self.assertEqual(self.preview(neighbour)["refusals"], ["target_unresolved"])  # no stored message for it

    def test_preview_writes_nothing_and_connects_nowhere(self):
        self.build_ledger()
        reply = self.draft()
        blocked = mock.patch.object(mail_poll, "connect", side_effect=AssertionError("connect"))
        with blocked:
            before = self.tree()
            self.assertEqual(self.preview(reply)["verdict"], rpp.WOULD_PLACE)
            self.assertEqual(self.tree(), before)
            self.assertFalse((self.project / "approvals" / md.JOURNAL).exists())
            self.assertFalse((self.project / "approvals" / md.LOCK).exists())
        md.place(self.project, self.config, factory=FakeIMAP().factory, now=NOW, only_id=reply["id"])
        other = self.draft(item="1" * 64)
        with blocked:
            before = self.tree()
            for row in (reply, other):
                self.preview(row)
            self.assertEqual(self.tree(), before)

    def test_without_config_no_message_identity_is_claimed(self):
        self.build_ledger()
        result = self.preview(self.draft(), config=None)
        self.assertEqual(result["verdict"], rpp.WOULD_PLACE)
        self.assertEqual((result["folder"], result["message_id"], result["fingerprint"]), (None, None, None))


class RefusalTests(Base):
    def test_template_residue_refuses_until_the_console_edit_and_a_fresh_approval(self):
        self.build_ledger()
        body = f"{draft_templates.DRAFT_BANNER}\n\nPlease extend. {draft_templates.EDIT.format('date')} {CANARY}\n"
        reply = self.draft(body=body)
        result = self.preview(reply)
        self.assertEqual(result["refusals"], ["body_has_draft_residue"])
        self.assertEqual(result["residue"], ["draft_banner", "edit_placeholder"])
        self.assertNotIn(CANARY, json.dumps(result))
        edited = aq.edit(self.project, reply["id"], "organizer", f"Please extend to 1 December. {CANARY}\n")
        self.assertEqual(self.preview(edited)["refusals"], ["not_approved"])
        approved = self.approve(reply["id"])
        self.assertEqual(self.preview(approved)["verdict"], rpp.WOULD_PLACE)
        # the earlier approved revision is stale even though the row is approved again
        self.assertEqual(self.preview(approved, revision=1)["refusals"], ["revision_stale"])

    def test_stale_decision_and_wrong_revision_refuse(self):
        self.build_ledger()
        reply = self.draft()
        edited = aq.edit(self.project, reply["id"], "organizer", "Edited after the approval.\n")
        self.assertEqual(self.preview(edited)["refusals"], ["not_approved"])
        self.assertEqual(self.preview(edited, revision=1)["refusals"], ["not_approved", "revision_stale"])
        self.assertEqual(self.preview(edited, revision=3)["refusals"], ["not_approved", "revision_ahead"])
        aq.reject(self.project, reply["id"], "organizer", "no")
        self.assertIn("not_approved", self.preview(reply, revision=3)["refusals"])

    def test_unverifiable_approval_refuses(self):
        self.build_ledger()
        reply = self.draft()
        stranger = Signer(trusted=False)
        self.addCleanup(stranger.close)
        with stranger.trust():
            result = self.preview(reply)
        self.assertEqual((result["verdict"], result["refusals"], result["status"]),
                         (rpp.REFUSED, ["approval_unverified"], "pending"))

    def test_ambiguous_or_unresolved_target_refuses(self):
        reply = self.draft()  # no ledger: the agency message cannot be resolved
        result = self.preview(reply)
        self.assertEqual(result["refusals"], ["target_unresolved"])
        self.assertEqual((result["threaded"], result["message_id"], result["fingerprint"]), (False, None, None))
        self.build_ledger()
        self.assertEqual(self.preview(reply)["verdict"], rpp.WOULD_PLACE)
        appeal = self.draft(kind="appeal-denial", body="Please reconsider.\n")  # a second reply to the same message
        self.assertEqual(self.preview(reply)["refusals"], ["target_shared"])
        self.assertEqual(self.preview(appeal)["refusals"], ["target_shared"])
        aq.edit(self.project, appeal["id"], "organizer", "Withdrawn back to pending.\n")
        self.assertEqual(self.preview(reply)["verdict"], rpp.WOULD_PLACE)

    def test_replies_to_different_attachments_or_kinds_of_one_message_share_its_target(self):
        doc2 = "c" * 64
        self.build_ledger(extra_docs=(doc2,))
        reply = self.draft()
        self.assertEqual(self.preview(reply)["verdict"], rpp.WOULD_PLACE)
        appeal = self.draft(item=received_items.item_id(doc2), kind="appeal-denial", body="Please reconsider.\n")
        self.assertEqual(self.preview(reply)["refusals"], ["target_shared"])
        self.assertEqual(self.preview(appeal)["refusals"], ["target_shared"])
        aq.edit(self.project, appeal["id"], "organizer", "Back to pending.\n")
        self.assertEqual(self.preview(reply)["verdict"], rpp.WOULD_PLACE)
        playbook = aq.add_draft(self.project, item_id="route:synthetic", kind="followup-playbook", agency_id="agency-1",
                                request_id="req-1", body="Please supply the logs.\n",
                                evidence={"vehicle": "reply_in_thread", "thread_item_id": received_items.item_id(doc2)},
                                source_run_id="run-1")
        playbook = self.approve(playbook["id"])
        self.assertEqual(self.preview(reply)["refusals"], ["target_shared"])
        self.assertEqual(self.preview(playbook)["refusals"], ["target_shared"])

    def test_resolved_thread_without_a_usable_address_has_no_target(self):
        self.build_ledger(headers="From: undisclosed-recipients:;\r\nSubject: Your request 4471\r\n"
                                  f"Message-ID: {AGENCY_MID}\r\n\r\nbody\r\n")
        result = self.preview(self.draft())
        self.assertEqual((result["verdict"], result["refusals"]), (rpp.REFUSED, ["recipient_missing"]))
        self.assertEqual((result["threaded"], result["to_domain"], result["fingerprint"]), (True, None, None))

    def test_unthreaded_new_request_without_recipient_refuses(self):
        row = aq.add_draft(self.project, item_id="route:synthetic", kind="followup-playbook", agency_id="alpha-pd",
                           request_id="OLD-1", body="Please supply the logs.\n",
                           evidence={"vehicle": "new_request", "thread_item_id": None}, source_run_id="r1")
        result = self.preview(self.approve(row["id"]))
        self.assertEqual((result["refusals"], result["threaded"], result["thread_reason"]),
                         (["recipient_missing"], False, "new_request"))

    def test_thread_provenance_refusal_is_listed_with_its_own_code(self):
        row = aq.add_draft(self.project, item_id="route:synthetic", kind="followup-playbook", agency_id="alpha-pd",
                           request_id="OLD-1", body="Please supply the logs.\n",
                           evidence={"vehicle": "reply_in_thread", "thread_item_id": "a" * 64}, source_run_id="r1")
        row = self.approve(row["id"])
        self.assertEqual(self.preview(row)["refusals"], ["target_unresolved", "thread_provenance_unresolved"])

    def test_inexact_unknown_or_invalid_requests_refuse(self):
        self.build_ledger()
        reply = self.draft()
        for queue_id, revision, code in ((reply["id"][:12], 1, ["queue_id_invalid"]),
                                         (reply["id"].upper(), 1, ["queue_id_invalid"]),
                                         (None, 1, ["queue_id_invalid"]),
                                         (reply["id"], -1, ["revision_invalid"]),
                                         (reply["id"], "1", ["revision_invalid"]),
                                         (reply["id"], True, ["revision_invalid"]),
                                         ("f" * 64, 0, ["queue_id_unknown"])):
            with self.subTest(queue_id=queue_id, revision=revision):
                result = rpp.preview(self.project, queue_id, revision, config=self.config)
                self.assertEqual((result["verdict"], result["refusals"]), (rpp.REFUSED, code))

    def test_review_copy_is_never_a_mail_draft(self):
        row = aq.add_draft(self.project, item_id="route:synthetic", kind="draft-agency-summary", agency_id="alpha-pd",
                           request_id="OLD-1", body="Summary for review.\n", evidence={}, source_run_id="r1")
        self.assertEqual(self.preview(row)["refusals"], ["not_mail_kind"])


class CommandLineTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rpp.main(["--project", str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes_and_content_free_output(self):
        self.build_ledger()
        reply = self.draft()
        code, out, _ = self.run_cli("--queue-id", reply["id"], "--revision", "1", "--config", str(self.config_path))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["verdict"], rpp.WOULD_PLACE)
        self.assertNotIn(CANARY, out)
        code, out, _ = self.run_cli("--queue-id", reply["id"], "--revision", "0")
        self.assertEqual((code, json.loads(out)["refusals"]), (2, ["revision_stale"]))
        bad = self.root / "private" / "open.json"
        bad.write_text("{}")
        bad.chmod(0o644)
        code, out, err = self.run_cli("--queue-id", reply["id"], "--revision", "1", "--config", str(bad))
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.strip())


if __name__ == "__main__":
    unittest.main()
