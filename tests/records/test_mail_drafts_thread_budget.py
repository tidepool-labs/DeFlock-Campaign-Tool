"""Exercise the read-only occurrence walk without opening a mailbox or queue."""
import sqlite3
import unittest

from campaign_tool.records import mail_drafts
from campaign_tool.records.ledger.migrations import v001

DOC, CONTAINER = "d" * 64, "c" * 64
FIRST_MAIL, SECOND_MAIL = "a" * 64, "b" * 64


class ThreadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(v001.SQL)
        self.db.executemany(
            "INSERT INTO originals(sha256,bytes,role,scope,preservation_status,provenance_json)"
            " VALUES(?,1,'agency_original','in_scope','verified','{}')",
            [(digest,) for digest in (DOC, CONTAINER, FIRST_MAIL, SECOND_MAIL)],
        )

    def add_mail(self, digest, uid):
        parent = f"mail-{uid}"
        self.db.execute("INSERT INTO occurrences VALUES(?,?,'mail','fixture',NULL,NULL,'fixture','{}')",
                        (parent, digest))
        self.db.execute("INSERT INTO mail_messages VALUES(?,'fixture','INBOX',1,?,NULL,?,'{}',NULL,'fixture')",
                        (parent, uid, digest))
        return parent

    def add_chain(self, branch, length, parent):
        for i in range(length):
            oid = f"{branch}-{i}"
            digest = DOC if i == length - 1 else CONTAINER
            self.db.execute("INSERT INTO occurrences VALUES(?,?,'attachment','fixture',?,NULL,'fixture','{}')",
                            (oid, digest, parent))
            parent = oid

    def test_incomplete_walk_refuses_first_mail(self):
        # Either visit order finds exactly one mail in 64 visits: the second
        # terminal mail is visit 65 (31 + 1 and 32 + 1 distinct occurrences).
        self.add_chain("left", 31, self.add_mail(FIRST_MAIL, 1))
        self.add_chain("right", 32, self.add_mail(SECOND_MAIL, 2))
        self.assertIsNone(mail_drafts._eml_for(self.db, DOC))

    def test_complete_unique_walk_resolves_at_exact_budget(self):
        # The 64th distinct visit leaves only an already checked shared mail
        # in pending. Neither hitting the cap nor that duplicate is a cutoff.
        parent = self.add_mail(FIRST_MAIL, 1)
        self.add_chain("left", 31, parent)
        self.add_chain("right", 32, parent)
        self.assertEqual(mail_drafts._eml_for(self.db, DOC), FIRST_MAIL)

    def test_complete_ambiguous_walk_refuses(self):
        # Both distinct mails are reached by visit 64; neither can win.
        self.add_chain("left", 31, self.add_mail(FIRST_MAIL, 1))
        self.add_chain("right", 31, self.add_mail(SECOND_MAIL, 2))
        self.assertIsNone(mail_drafts._eml_for(self.db, DOC))
