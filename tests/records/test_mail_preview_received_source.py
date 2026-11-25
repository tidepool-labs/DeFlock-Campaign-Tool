"""D36: a received-item reply's review page names the cited passage it was drafted from (synthetic, offline).

draft_templates.render keeps the locator in evidence["kind_basis"]; before D36 the "Cited records" block read only
evidence["citations"] and said "Sources unknown" for every received-item reply, while the locator sat in the body.
"""
import html
import re

from campaign_tool.records import approval_queue as aq, draft_templates as dt, mail_preview
from campaign_tool.records import console_snapshot as cs
from tests.records.test_approval_drafts import item
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import Panel

PART_QUOTE = "We need 14 more days <to respond> & will write by Dec 1."
PAGE_QUOTE = "The duplication fee is $4."


def section(page):
    return re.search(r'<section class="review-context">.*?</section>', page, re.S)[0]


def mail_item(n, quote=PART_QUOTE, part="1"):
    out = item(n, "extension", quote=quote)
    out["kind_basis"] = {"page": None, "part": part, "quote": quote}
    return out


class ReceivedSourceTests(Panel):
    def queue(self, received):
        kind, body, evidence = dt.render(received)
        return aq.add_draft(self.live, item_id=received["item_id"], kind=kind, agency_id=received["agency_id"],
                            request_id=received["request_id"], body=body, evidence=evidence, source_run_id=RUN)

    def test_part_and_page_citations_show_their_own_locator_live_and_sealed(self):
        mail_row = self.queue(mail_item(301))
        pdf_row = self.queue(item(302, "fee", quote=PAGE_QUOTE))
        live = {r["id"]: cs.LiveSource(self.live).review_context(r["id"]) for r in (mail_row, pdf_row)}
        self.assertEqual(live[mail_row["id"]]["received_source"],
                         {"item_id": mail_row["item_id"], "page": None, "part": "1", "quote": PART_QUOTE})
        self.assertEqual(live[pdf_row["id"]]["received_source"],
                         {"item_id": pdf_row["item_id"], "page": 1, "quote": PAGE_QUOTE})
        self.make_export()
        source = cs.SnapshotSource(cs.Pinner(str(self.root)).pin())
        self.up()
        for row, where in ((mail_row, "message part 1 (an email text part; it has no page number)"),
                           (pdf_row, "page 1")):
            with self.subTest(kind=row["kind"]):
                self.assertEqual(source.review_context(row["id"]), live[row["id"]])  # sealed == live
                block = section(self.get("/review?id=" + row["id"]))
                self.assertEqual(block, mail_preview.context_html(live[row["id"]]))
                text = html.unescape(re.sub(r"<[^>]+>", "", block))
                self.assertIn("This reply answers received item %s, %s." % (row["item_id"][:12], where), text)
                self.assertIn(dict(row["evidence"]["kind_basis"])["quote"], text)
                self.assertNotIn("Sources unknown", block)
        mail_block = section(self.get("/review?id=" + mail_row["id"]))
        self.assertNotIn("page 1", mail_block)  # a mail body is never given a page
        self.assertNotIn("page None", mail_block)
        self.assertIn("&lt;to respond&gt; &amp; will", mail_block)  # the quote is escaped, never markup
        self.assertNotIn("<to respond>", mail_block)

    def test_rows_without_a_received_item_citation_keep_the_old_wording(self):
        context = cs.LiveSource(self.live).review_context(self.pending_id)  # evidence {"source": "test"}
        self.assertIsNone(context["received_source"])
        self.assertIn("Sources unknown: producer supplied no citation list.", mail_preview.context_html(context))
        for evidence in ({}, None, {"kind_basis": {"page": None, "quote": None}, "item_id": "a" * 64}):
            with self.subTest(evidence=evidence):
                self.assertIsNone(mail_preview.received_source(evidence))
        old = {k: v for k, v in context.items() if k != "received_source"}  # an export from before D36
        self.assertEqual(mail_preview.context_html(old), mail_preview.context_html(context))

    def test_malformed_citations_are_flagged_never_repaired_or_paged(self):
        good = {"item_id": "b" * 64, "kind_basis": {"page": None, "part": "1", "quote": "We deny it."}}
        broken = {
            "page and part": {"page": 2, "part": "1", "quote": "We deny it."},
            "blank quote": {"page": None, "part": "1", "quote": "  "},
            "int part": {"page": None, "part": 1, "quote": "We deny it."},
            "page zero": {"page": 0, "quote": "We deny it."},
            "bool page": {"page": True, "quote": "We deny it."},
            "extra key": {"page": 1, "quote": "We deny it.", "url": "https://example.test"},
            "smuggled item id": {"page": 1, "quote": "We deny it.", "item_id": "c" * 64},
            "part without page key": {"part": "1", "quote": "We deny it."},
            "quote only": {"quote": "We deny it."},
            "not a dict": "page 1",
        }
        self.assertTrue(mail_preview._received_ok(mail_preview.received_source(good)))
        cases = [{**good, "kind_basis": basis} for basis in broken.values()]
        cases += [{**good, "item_id": bad} for bad in (None, "B" * 64, "b" * 63)]
        for evidence in cases:
            with self.subTest(evidence=evidence):
                found = mail_preview.received_source(evidence)
                self.assertEqual(found, {"invalid": True})
                rendered = mail_preview.context_html({"received_source": found, "citations": None})
                self.assertIn("Received-item citation unavailable", rendered)
                self.assertNotIn("This reply answers", rendered)
                self.assertNotIn("Sources unknown", rendered)
        # a hand-edited export object is re-checked at render time, not trusted
        for forged in ({"item_id": "b" * 64, "page": None, "quote": "x"}, {"item_id": "b" * 64, "page": 3},
                       {"item_id": "b" * 64, "part": "3", "quote": "x"}, ["b" * 64, 3, "x"], "page 3"):
            with self.subTest(forged=forged):
                rendered = mail_preview.context_html({"received_source": forged, "citations": None})
                self.assertIn("Received-item citation unavailable", rendered)
                self.assertNotIn("This reply answers", rendered)
                self.assertNotIn("page 3", rendered)
                self.assertNotIn("message part 3", rendered)

    def test_supplied_citation_list_is_still_shown_beside_the_received_item(self):
        context = {"received_source": {"item_id": "d" * 64, "page": 4, "quote": "Fee: $4."},
                   "citations": [{"source_type": "tracker", "locator": "row 9"}]}
        rendered = mail_preview.context_html(context)
        self.assertIn("received item <code>dddddddddddd</code>, page 4.", rendered)
        self.assertIn("<li>source_type: tracker; locator: row 9</li>", rendered)
