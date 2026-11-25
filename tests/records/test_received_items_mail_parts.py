"""REC-MAIL-CITE-1: an unpaginated agency mail body is read and cited by its MIME part, never an invented page.

Synthetic only. Reuses the producer-shaped ledger/extraction helpers of the received-items tests without
collecting their tests twice.
"""
import json
import sqlite3
import unittest

from campaign_tool.records import approval_queue as aq, draft_templates as dt, run_stages
from campaign_tool.records.extraction_ledger import canonical, sha
from campaign_tool.records import extraction_routes as routes
from campaign_tool.records.outcomes import received_items as ri
from tests.records import test_approval_drafts as drafts
from tests.records import test_outcome_received_items as fixtures

HEADERS = "From: records@agency.example\nSubject: Your request\nWe deny your request."  # never read: headers
EXTENSION = ("<div>Caltrans is exercising its authority under Government Code section 7922.535 "
             "to extend the time. You will receive a response no later than November 30, 2026</div>")
EXTENSION_LINE = EXTENSION  # one line: the exact quoted source line


class MailPartCitationTests(unittest.TestCase):
    project = processing = database = None
    setUp = fixtures.ReceivedItemsTests.setUp
    private = fixtures.ReceivedItemsTests.private
    document = fixtures.ReceivedItemsTests.document
    collect = fixtures.ReceivedItemsTests.collect
    item = fixtures.ReceivedItemsTests.item

    def mail(self, units, agency="caltrans"):
        """Enroll one synthetic message original whose extraction units are [(kind, locator, text)]."""
        subject, source = self.document(["placeholder"], agency, raw=("SYNTHETIC MAIL %d\n" % self.serial).encode()
                                        + json.dumps(units).encode())
        self.serial += 1
        run = self.project / ("extraction-mail-" + str(self.serial))
        payloads = [{"kind": kind, "locator": locator, "text": text, "data": {}} for kind, locator, text in units]
        pages = sum("page" in u["locator"] for u in payloads)
        metadata = {"stage": "complete", "parser": "fixture-mail", "parser_version": "synthetic-1",
                    "counts": {"pages_expected": pages}, "children": []}
        self.private(run / "input", source.read_bytes())
        self.private(run / "derived" / "digest.json", canonical(metadata).encode())
        self.private(run / "derived" / "units.jsonl", b"".join((canonical(u) + "\n").encode() for u in payloads))
        receipt = {"schema": "format-extraction-v1", "original_sha256": subject, "route": routes.route("pdf"),
                   "parser_adapter": routes.VERSION, "status": "partial", "units": payloads,
                   "pages": routes.page_manifest(payloads, pages, "fixture-mail", "synthetic-1"), "issues": [],
                   "review_status": "not_reviewed", "original_changed": False, "run_path": str(run),
                   "parser": "fixture-mail", "parser_version": "synthetic-1", "parser_components": {}, "children": []}
        self.private(run / "extraction.json", (canonical(receipt) + "\n").encode())
        self.adapter.enroll(original_path=source, receipt_path=run / "extraction.json",
                            receipt_sha256=sha((run / "extraction.json").read_bytes()))
        return subject

    def live_shape(self, body, part="1"):
        return [("email_headers", {"mime": "message/rfc822"}, HEADERS),
                ("mime_part", {"mime": "text/html", "part": part}, ""),
                ("email_body", {"mime": "text/html", "part": part}, body)]

    def test_live_mail_shape_extension_is_classified_and_cited_by_its_part(self):
        subject = self.mail(self.live_shape(EXTENSION))
        got = self.item(self.collect(), subject)
        self.assertEqual(got["kind"], "extension")
        self.assertEqual(got["kind_basis"], {"page": None, "part": "1", "quote": EXTENSION_LINE})
        self.assertEqual([(d["date"], d["page"], d["part"]) for d in got["deadlines"]], [("2026-11-30", None, "1")])
        self.assertNotIn("page_locator_unavailable", got["diagnostics"])
        self.assertEqual([(c["kind"], c["page"], c["part"]) for c in got["candidates"]], [("extension", None, "1")])
        self.assertTrue(got["review_required"] and not got["legal_conclusion"])

    def test_mail_intake_source_part_label_is_the_citation(self):
        units = [("email_body", {"mime": "text/plain", "source_part": "0|2"}, "We deny your request.")]
        subject = self.mail(units)
        got = self.item(self.collect(), subject)
        self.assertEqual((got["kind"], got["kind_basis"]), ("denial", {"page": None, "part": "0|2",
                                                                        "quote": "We deny your request."}))

    def test_headers_history_and_non_mail_units_are_never_cited_by_part(self):
        cases = {
            "headers only": [("email_headers", {"mime": "message/rfc822"}, HEADERS)],
            "forwarded history": [("email_body", {"mime": "text/plain", "part": "1"},
                                   "Thanks.\n-----Original Message-----\nWe deny your request.")],
            "document kind, mail locator": [("pdf_page", {"mime": "text/html", "part": "1"}, "We deny your request.")],
            "extra locator key": [("email_body", {"mime": "text/plain", "part": "1", "line": 3}, "We deny your request.")],
            "two part labels": [("email_body", {"mime": "text/plain", "part": "1", "source_part": "1"},
                                 "We deny your request.")],
            "control in label": [("email_body", {"mime": "text/plain", "part": "1\n2"}, "We deny your request.")],
            "whitespace-only label": [("email_body", {"mime": "text/plain", "part": "   "}, "We deny your request.")],
            "whitespace-only source_part": [("mime_part", {"mime": "text/plain", "source_part": "\u00a0\t"},
                                             "We deny your request.")],
            "C1 control in label": [("email_body", {"mime": "text/plain", "part": "1\x85"}, "We deny your request.")],
            "no mime": [("email_body", {"part": "1"}, "We deny your request.")],
        }
        subjects = {name: self.mail(units) for name, units in cases.items()}
        report = self.collect()
        for name, subject in subjects.items():
            with self.subTest(name):
                got = self.item(report, subject)
                self.assertEqual((got["kind"], got["kind_basis"]), ("other", {"page": None, "quote": None}))
                self.assertFalse(any("part" in d for d in got["deadlines"]))

    def test_a_mail_unit_with_a_real_page_keeps_the_page_citation(self):
        subject = self.mail([("email_body", {"mime": "text/plain", "part": "1", "page": 1}, "We deny your request.")])
        got = self.item(self.collect(), subject)
        self.assertEqual((got["kind"], got["kind_basis"]), ("denial", {"page": 1, "quote": "We deny your request."}))

    def test_other_readers_keep_the_page_contract(self):
        subject = self.mail(self.live_shape(EXTENSION))
        with run_stages.snapshot_ro(self.database) as con:
            con.row_factory = sqlite3.Row
            units, diagnostics, _ = ri._current_units(con, self.processing, subject)
        self.assertEqual((units, diagnostics), ([], ["page_locator_unavailable"]))  # default call: unchanged

    def test_unknown_page_without_a_part_still_refuses(self):
        with self.assertRaisesRegex(ValueError, "received_items_basis"):
            ri.classify([(None, 1, "u1", "We deny your request.")], {"other-unit": "1"})


def mail_item(n=1, kind="extension", part="1", quote=EXTENSION_LINE, deadlines=()):
    out = drafts.item(n, kind, deadlines=deadlines, quote=quote)
    out["kind_basis"] = {"page": None, "part": part, "quote": quote}
    return out


class MailPartDraftTests(unittest.TestCase):
    def test_reply_extension_quotes_the_message_part_and_keeps_it_in_evidence(self):
        deadline = {"date_text": "November 30, 2026", "date": "2026-11-30", "page": None, "part": "1",
                    "quote": EXTENSION_LINE, "legal_deadline_calculation": False}
        kind, body, evidence = dt.render(mail_item(deadlines=[deadline]))
        self.assertEqual(kind, "reply-extension")
        self.assertIn("The agency wrote (message part 1):\n\n    | " + EXTENSION_LINE, body)
        self.assertNotIn("page None", body)
        self.assertEqual(evidence["kind_basis"], {"page": None, "part": "1", "quote": EXTENSION_LINE})
        self.assertEqual(evidence["deadlines"], [{"date_text": "November 30, 2026", "date": "2026-11-30",
                                                  "page": None, "quote": EXTENSION_LINE, "part": "1"}])
        self.assertIn(dt.DRAFT_BANNER, body)

    def test_appeal_denial_names_the_part(self):
        kind, body, _ = dt.render(mail_item(2, "denial", quote="We deny your request."))
        self.assertEqual(kind, "appeal-denial")
        self.assertIn("as quoted from message part 1:", body)
        self.assertNotIn("page None", body)

    def test_paged_citations_render_exactly_as_before(self):
        kind, body, evidence = dt.render(drafts.item(3, "fee", quote="The duplication fee is $4."))
        self.assertEqual(kind, "fee-reply")
        self.assertIn("The agency wrote (page 1):\n\n    | The duplication fee is $4.", body)
        self.assertEqual(evidence["kind_basis"], {"page": 1, "quote": "The duplication fee is $4."})

    def test_malformed_part_citations_are_refused(self):
        broken = {
            "page and part": {"page": 1, "part": "1", "quote": "We deny your request."},
            "empty part": {"page": None, "part": "", "quote": "We deny your request."},
            "int part": {"page": None, "part": 1, "quote": "We deny your request."},
            "control part": {"page": None, "part": "1\x00", "quote": "We deny your request."},
            "whitespace-only part": {"page": None, "part": "   ", "quote": "We deny your request."},
            "unicode-space part": {"page": None, "part": "\u00a0\u2003", "quote": "We deny your request."},
            "C1 control part": {"page": None, "part": "1\x85", "quote": "We deny your request."},
            "blank quote": {"page": None, "part": "1", "quote": "  "},
        }
        for name, basis in broken.items():
            with self.subTest(name), self.assertRaisesRegex(dt.DraftSourceError, "^kind_basis$"):
                dt.render({**drafts.item(4, "denial"), "kind_basis": basis})
        other = {**drafts.item(5, "other"), "kind_basis": {"page": None, "part": "1", "quote": None}}
        with self.assertRaisesRegex(dt.DraftSourceError, "^kind_basis$"):
            dt.validate_item(other)
        bad_deadline = {"date_text": "November 30", "date": None, "page": 2, "part": "1", "quote": "x by November 30"}
        with self.assertRaisesRegex(dt.DraftSourceError, "^deadline$"):
            dt.validate_item(mail_item(6, deadlines=[bad_deadline]))
        blank_deadline = {"date_text": "November 30", "date": None, "page": None, "part": "  ",
                          "quote": "x by November 30"}
        with self.assertRaisesRegex(dt.DraftSourceError, "^deadline$"):
            dt.validate_item(mail_item(7, deadlines=[blank_deadline]))


class MailPartQueueTests(unittest.TestCase):
    project = processing = database = None
    setUp = fixtures.ReceivedItemsTests.setUp
    private = fixtures.ReceivedItemsTests.private
    document = fixtures.ReceivedItemsTests.document
    collect = fixtures.ReceivedItemsTests.collect
    item = fixtures.ReceivedItemsTests.item
    mail = MailPartCitationTests.mail
    live_shape = MailPartCitationTests.live_shape

    def test_tied_mail_extension_queues_one_pending_cited_reply_and_nothing_is_sent(self):
        subject = self.mail(self.live_shape(EXTENSION))
        report = self.collect()
        got = dict(self.item(report, subject), agency_id="caltrans", request_id="REQ-1")  # as an applied tie gives
        root = self.project / "queue-root"
        root.mkdir(mode=0o700)
        path = root / "received.json"
        path.write_text(json.dumps(drafts.received([got])), encoding="utf-8")
        self.assertEqual(aq.queue_received_items(root, "run-1", path), {"created": 1, "existing": 0, "skipped": 0})
        rows = aq.load_queue(root)
        self.assertEqual([(r["kind"], r["status"]) for r in rows], [("reply-extension", "pending")])
        self.assertIn("message part 1", rows[0]["body"])
        unlinked = self.item(report, subject)  # the same item before any tie: no draft
        self.assertIsNone(dt.render(unlinked))


if __name__ == "__main__":
    unittest.main()
