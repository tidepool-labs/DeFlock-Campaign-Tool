"""DF1125-01: an accepted correction request names the correction that answered it (``--correction-date``).

Synthetic campaign (the correction-request fixture), offline. Nothing is sent or published.
Run from the repository root: python3 -B -m unittest tests.test_correction_request_link -v
"""
import hashlib
import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import correction_requests as cr, findings  # noqa: E402
from campaign_tool.content import ContentError  # noqa: E402
from tests import test_correction_requests as base  # noqa: E402  (module import: not re-collected here)

FID, TODAY = base.FID, base.TODAY
BANNER = "DRAFT — NOT SENT. Review and edit before sending it yourself."
URL = "https://example.org/findings/synthetic-title.html"
CONFIRM_DATE = "[EDIT ME: confirm the correction date]"
CONFIRM_ADDRESSED = "[EDIT ME: confirm the correction addressed this request]"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def draft(*body):
    return "\n".join(["# Draft reply to correction request 1", "", BANNER, "",
                      "[EDIT ME: add greeting outside this repository]", "", f"Subject: finding {FID}", "", *body,
                      "", f"Finding: {URL}", "", "[EDIT ME: sign-off]", ""])


class CorrectionLinkTests(base.CorrectionRequestTests):
    def setUp(self):
        super().setUp()
        self.reply_file = self.root / "kit" / "corrections" / "replies" / "1.md"
        site = self.root / "content" / "site.json"
        document = json.loads(site.read_text())
        document["base_url"] = "https://example.org"
        site.write_text(json.dumps(document))

    def correct(self, day, note="Fixed the quote."):
        findings.correct(self.root, FID, day, note, now=datetime(2026, 3, 19, tzinfo=timezone.utc),
                         approved_by="Pat Organizer")

    def accept(self, on="2026-03-10", correction_date=None, request_id=1):
        extra = {} if correction_date is None else {"correction_date": correction_date}  # no flag = main's call
        return cr.decide(self.root, request_id, "accepted", on, "Quote fixed.", today=TODAY, **extra)

    def reply_text(self):
        if self.reply_file.exists():
            self.reply_file.unlink()
        cr.reply(self.root, 1)
        return self.reply_file.read_text(encoding="utf-8")

    def rewrite_log(self, change):
        document = json.loads(self.file.read_text())
        change(document["requests"][0])
        self.file.write_text(json.dumps(document))

    def test_linked_accept_via_cli_stores_the_link_and_its_reply_names_the_correction(self):
        self.add(received="2026-03-06")
        self.correct("2026-03-07", "Fixed the quote.")
        self.correct("2026-03-08", "Replaced the page 2 excerpt.")
        result = self.cli("correction-request", "decide", "accepted", "--directory", self.root, "--id", "1",
                          "--on", "2026-03-10", "--note", "Quote fixed.", "--correction-date", "2026-03-08")
        stored = json.loads(self.file.read_text())["requests"][0]
        link = {"date": "2026-03-08", "note_sha256": sha("Replaced the page 2 excerpt.")}
        self.assertEqual(stored["correction"], link)
        self.assertEqual(json.loads(result.stdout.rsplit("\n", 2)[0])["correction"], link)
        self.assertEqual(cr.list_requests(self.root, TODAY)[0]["correction"], link)
        self.assertEqual(self.reply_text(), draft(
            "We accepted your correction request about this finding.", "",
            "Received on: 2026-03-06", "Decision date: 2026-03-10", "Correction date: 2026-03-08", "",
            "Correction note:", "> Replaced the page 2 excerpt.", "", "Recorded decision note:", "> Quote fixed."))
        # two corrections in the window: without a link the draft could not pick; the link does
        self.assertNotIn(CONFIRM_DATE, self.reply_text())
        self.assertNotIn(CONFIRM_ADDRESSED, self.reply_text())
        # the link changes no count
        self.assertEqual(cr.quarterly_counts(self.root, TODAY),
                         [{"quarter": "2026-Q1", "received": 1, "accepted": 1, "declined": 0, "still_open": 0}])

    def test_zero_or_several_corrections_that_day_are_refused_naming_the_count(self):
        self.add(received="2026-03-06")
        self.correct("2026-03-07")
        self.correct("2026-03-07", "Second fix the same day.")
        self.refused("the finding has 2 corrections dated that day; exactly one is needed",
                     self.accept, correction_date="2026-03-07")
        self.refused("the finding has 0 corrections dated that day; exactly one is needed",
                     self.accept, correction_date="2026-03-09")
        self.assertEqual(json.loads(self.file.read_text())["requests"][0]["state"], "open")

    def test_a_finding_with_no_corrections_is_refused_naming_the_count_of_zero(self):
        self.add(received="2026-03-06")
        self.refused("--correction-date 2026-03-08: the finding has 0 corrections dated that day; exactly one is needed",
                     self.accept, correction_date="2026-03-08")
        before = self.file.read_bytes()
        result = self.cli("correction-request", "decide", "accepted", "--directory", self.root, "--id", "1",
                          "--on", "2026-03-10", "--note", "Quote fixed.", "--correction-date", "2026-03-08",
                          returncode=3)
        self.assertIn("the finding has 0 corrections dated that day; exactly one is needed", result.stderr)
        self.assertNotIn("accept only after recording the correction", result.stderr)
        self.assertEqual(self.file.read_bytes(), before)
        # an unlinked accept keeps the old guard and its wording
        self.refused("accept only after recording the correction: finding correct", self.accept)
        self.assertEqual(json.loads(self.file.read_text())["requests"][0]["state"], "open")

    def test_a_date_outside_the_window_or_not_a_date_is_refused(self):
        self.add(received="2026-03-06")
        self.correct("2026-03-05")  # before the request
        self.correct("2026-03-12")  # after the decision date below
        self.refused("--correction-date 2026-03-05 is outside this request's window (2026-03-06 through the "
                     "decision date 2026-03-10)", self.accept, correction_date="2026-03-05")
        self.refused("--correction-date 2026-03-12 is outside this request's window",
                     self.accept, correction_date="2026-03-12")
        self.refused("--correction-date 2026-3-12: not a real calendar date", self.accept, correction_date="2026-3-12")
        self.refused("--correction-date 2026-03-21 is in the future", self.accept, correction_date="2026-03-21")
        self.refused("--correction-date is only for accepted", cr.decide, self.root, 1, "declined", "2026-03-10",
                     "No.", today=TODAY, correction_date="2026-03-12")
        result = self.cli("correction-request", "decide", "declined", "--directory", self.root, "--id", "1",
                          "--on", "2026-03-10", "--note", "No.", "--correction-date", "2026-03-12", returncode=3)
        self.assertIn("--correction-date is only for accepted", result.stderr)
        self.assertEqual(json.loads(self.file.read_text())["requests"][0]["state"], "open")

    def test_accept_without_the_flag_and_an_older_log_are_unchanged(self):
        self.add(received="2026-03-06")
        self.correct("2026-03-08")
        self.accept()
        stored = json.loads(self.file.read_text())["requests"][0]
        self.assertEqual(stored, {"id": 1, "finding_id": FID, "received_on": "2026-03-06",
                                  "summary": "Page 2 is misquoted.", "state": "accepted", "decided_on": "2026-03-10",
                                  "note": "Quote fixed."})
        self.assertEqual(self.reply_text(), draft(
            "We accepted your correction request about this finding.", "",
            "Received on: 2026-03-06", "Decision date: 2026-03-10", "Correction date: 2026-03-08", "",
            "Recorded decision note:", "> Quote fixed.", "", CONFIRM_ADDRESSED))
        self.assertEqual(cr.quarterly_counts(self.root, TODAY),
                         [{"quarter": "2026-Q1", "received": 1, "accepted": 1, "declined": 0, "still_open": 0}])

    def test_a_changed_or_removed_linked_correction_is_flagged_in_the_reply(self):
        self.add(received="2026-03-06")
        self.correct("2026-03-08")
        self.accept(correction_date="2026-03-08")
        log = self.file.read_bytes()
        gone = "Correction date: [EDIT ME: the linked correction dated 2026-03-08 is no longer on the finding; confirm it]"
        # the note on the finding was changed by hand after the link was recorded
        path = next(p for p in sorted((self.root / "content" / "findings").glob("*.json"))
                    if json.loads(p.read_text()).get("id") == FID)
        document = json.loads(path.read_text())
        document["corrections"][0]["note"] = "A different note."
        path.write_text(json.dumps(document))
        text = self.reply_text()
        self.assertIn(gone + "\n\nRecorded decision note:\n> Quote fixed.\n", text)
        for absent in ("Correction note:", "A different note.", CONFIRM_DATE, CONFIRM_ADDRESSED):
            self.assertNotIn(absent, text)
        self.assertEqual(self.file.read_bytes(), log)  # reply never changes the log

    def test_malformed_links_are_refused_by_field(self):
        self.add(received="2026-03-06")
        self.correct("2026-03-08")
        self.accept()
        good = self.file.read_bytes()
        cases = (
            ("must be an object with exactly date and note_sha256", lambda r: r.update(correction="2026-03-08")),
            ("must be an object with exactly date and note_sha256",
             lambda r: r.update(correction={"date": "2026-03-08", "note_sha256": sha("x"), "extra": 1})),
            ("must be an object with exactly date and note_sha256", lambda r: r.update(correction={"date": "2026-03-08"})),
            ("'correction.date' in requests[0] is not a YYYY-MM-DD date",
             lambda r: r.update(correction={"date": "2026-3-8", "note_sha256": sha("x")})),
            ("'correction.note_sha256' in requests[0] is not a sha256 hex digest",
             lambda r: r.update(correction={"date": "2026-03-08", "note_sha256": sha("x").upper()})),
            ("'correction.note_sha256' in requests[0] is not a sha256 hex digest",
             lambda r: r.update(correction={"date": "2026-03-08", "note_sha256": 7})),
            # the whole value must match: a trailing newline is not a digest (or a date)
            ("'correction.note_sha256' in requests[0] is not a sha256 hex digest",
             lambda r: r.update(correction={"date": "2026-03-08", "note_sha256": sha("Fixed the quote.") + "\n"})),
            ("'correction.date' in requests[0] is not a YYYY-MM-DD date",
             lambda r: r.update(correction={"date": "2026-03-08\n", "note_sha256": sha("Fixed the quote.")})),
            ("requests[0] has a 'correction' link but is not accepted",
             lambda r: r.update(state="declined", correction={"date": "2026-03-08", "note_sha256": sha("x")})),
        )
        for needle, change in cases:
            with self.subTest(needle=needle):
                self.file.write_bytes(good)
                self.rewrite_log(change)
                with self.assertRaises(ValueError) as caught:
                    cr.list_requests(self.root, TODAY)
                self.assertIn(needle, str(caught.exception))
                self.refused(needle, cr.add, self.root, FID, "2026-03-07", "Another ask.", today=TODAY)
                with self.assertRaises(ValueError) as caught:
                    cr.reply(self.root, 1)
                self.assertIn("missing, unreadable or invalid", str(caught.exception))
                with self.assertRaises(ContentError):
                    cr.quarterly_counts(self.root, TODAY)
                self.assertFalse(self.reply_file.exists())
        # an open request with a link is refused too (it has no decision yet)
        self.file.write_bytes(good)
        self.rewrite_log(lambda r: (r.update(state="open", correction={"date": "2026-03-08", "note_sha256": sha("x")}),
                                    r.pop("decided_on"), r.pop("note")))
        with self.assertRaises(ValueError) as caught:
            cr.list_requests(self.root, TODAY)
        self.assertIn("requests[0] has a 'correction' link but is not accepted", str(caught.exception))

    def test_open_and_declined_replies_are_unchanged(self):
        self.add(received="2026-03-06")
        self.assertEqual(self.reply_text(), draft(
            "We received your correction request about this finding on 2026-03-06. It is being reviewed, and we will "
            "write again when a decision has been recorded."))
        cr.decide(self.root, 1, "declined", "2026-03-08", "Records support the text.", today=TODAY)
        self.assertEqual(self.reply_text(), draft(
            "We reviewed your correction request about this finding and did not accept it.", "",
            "Received on: 2026-03-06", "Decision date: 2026-03-08", "", "Reason recorded:",
            "> Records support the text."))
        self.assertEqual(date.fromisoformat(cr.list_requests(self.root, TODAY)[0]["decided_on"]), date(2026, 3, 8))


for _name in [n for n in dir(base.CorrectionRequestTests) if n.startswith("test_")]:
    setattr(CorrectionLinkTests, _name, None)  # inherited tests belong to tests.test_correction_requests

if __name__ == "__main__":
    unittest.main()
