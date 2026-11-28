"""DF1125-04 slice 2: `finding reply-asked` keeps a private record of the day the agency was really asked
(private/reply-asks.json); the draft is never rewritten, `finding publish` says whether that record exists, a broken
record refuses, and `finding reply` must agree with it. Synthetic data only; nothing is sent and no socket is opened.

Run from the repository root: python3 -B -m unittest tests.test_finding_reply_asked -v
"""
import json
import os
import stat
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from tests import test_finding_reply as reply_tests  # noqa: E402  (module import: not re-collected here)

TODAY = date.today()
SOON = TODAY + timedelta(days=5)
SLUG = reply_tests.SLUG
ID = "syn-pub-1"


class ReplyAskedTests(reply_tests.ReplyBase):
    def pending(self, prepared=TODAY):
        self.request(by=SOON, today=prepared)
        self.approve()  # after the request: preparing it changes the reviewed content

    def asks(self):
        return self.root / "private" / "reply-asks.json"

    def published(self):
        return self.content / "findings" / f"{SLUG}.json"

    def test_the_asking_record_is_private_and_leaves_the_reviewed_draft_alone(self):
        self.pending()
        draft_before, status_before = self.draft.read_bytes(), self.status()
        result = findings.record_asked(self.root, self.draft, str(TODAY), today=TODAY)
        self.assertEqual(result, {"id": ID, "asked_on": str(TODAY), "reply_by": str(SOON)})
        self.assertEqual(json.loads(self.asks().read_text(encoding="utf-8")),
                         {ID: {"asked_on": str(TODAY), "reply_by": str(SOON)}})
        self.assertEqual(stat.S_IMODE(self.asks().stat().st_mode), 0o600)
        self.assertEqual(self.draft.read_bytes(), draft_before)  # receipts stay current
        self.assertEqual(self.status(), status_before)
        again = findings.record_asked(self.root, self.draft, str(TODAY), today=TODAY)
        self.assertEqual(again, {"id": ID, "asked_on": str(TODAY), "unchanged": True})
        self.refused(f"an asking record already says you asked on {TODAY}", findings.record_asked, self.root,
                     self.draft, str(SOON), today=SOON)

    def test_publish_says_whether_an_asking_record_exists_and_still_waits(self):
        self.pending()
        err = self.refused(f"until {SOON} to reply", findings.publish, self.root, self.draft, "Pat Organizer")
        self.assertIn("no asking record exists", err)
        findings.record_asked(self.root, self.draft, str(TODAY), today=TODAY)
        err = self.refused(f"until {SOON} to reply", findings.publish, self.root, self.draft, "Pat Organizer")
        self.assertIn(f"the asking record says you asked on {TODAY}. Wait until {SOON}", err)
        self.assertFalse(self.published().exists())
        findings.publish(self.root, self.draft, "Pat Organizer", before_reply_deadline="Vote moved up.")
        self.assertTrue(self.published().exists())

    def test_asked_on_must_lie_from_the_prepared_day_through_the_deadline_and_not_after_today(self):
        self.refused("prepare a reply request first", findings.record_asked, self.root, self.draft, str(TODAY),
                     today=TODAY)
        self.pending()
        for asked, today, needle in (
                (TODAY - timedelta(days=1), TODAY, f"from the request's prepared day ({TODAY}) through its reply "
                                                   f"deadline ({SOON})"),
                (TODAY + timedelta(days=1), TODAY, "not after today"),
                (SOON + timedelta(days=1), SOON + timedelta(days=2), f"through its reply deadline ({SOON})"),
                ("2026-02-30", TODAY, "must be a real YYYY-MM-DD date")):
            with self.subTest(asked=asked):
                self.refused(needle, findings.record_asked, self.root, self.draft, str(asked), today=today)
        self.assertFalse(self.asks().exists())
        self.reply("declined", on=TODAY, asked=TODAY, text=None)
        self.refused("a reply is already recorded", findings.record_asked, self.root, self.draft, str(TODAY),
                     today=TODAY)

    def test_a_broken_asking_record_refuses_publish_naming_the_file_and_field(self):
        self.pending()
        good = {"asked_on": str(TODAY), "reply_by": str(SOON)}
        cases = (("not JSON", "{MARKER-9Z", "asking record is not valid JSON"),
                 ("a list", [good], "must be a JSON object keyed by finding id"),
                 ("a bad key", {"../MARKER-9Z": good}, "must be a JSON object keyed by finding id"),
                 ("a missing field", {ID: {"asked_on": str(TODAY)}}, f"entry {ID} must have exactly asked_on and "
                                                                     "reply_by"),
                 ("a bad day", {ID: dict(good, asked_on="MARKER-9Z")}, f"entry {ID} field asked_on must be a real"),
                 ("another deadline", {ID: dict(good, reply_by=str(SOON + timedelta(days=1)))},
                  f"entry {ID} field reply_by ({SOON + timedelta(days=1)}) is not the draft's reply deadline "
                  f"({SOON})"))
        for what, value, needle in cases:
            with self.subTest(what=what):
                self.asks().write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
                for extra in ({}, {"before_reply_deadline": "Vote moved up."}):
                    err = self.refused(needle, findings.publish, self.root, self.draft, "Pat Organizer", **extra)
                    self.assertIn("reply-asks.json", err)
                    self.assertNotIn("MARKER-9Z", err)
                self.refused(needle, findings.record_asked, self.root, self.draft, str(TODAY), today=TODAY)
                self.asks().unlink()
        for kind in ("symlink", "folder"):
            with self.subTest(what=kind):
                if kind == "symlink":
                    target = self.base / "elsewhere.json"
                    target.write_text(json.dumps({ID: good}), encoding="utf-8")
                    os.symlink(target, self.asks())
                else:
                    self.asks().mkdir()
                self.refused("reply-asks.json", findings.publish, self.root, self.draft, "Pat Organizer",
                             before_reply_deadline="Vote moved up.")
                self.asks().unlink() if kind == "symlink" else self.asks().rmdir()
        self.assertFalse(self.published().exists())

    def test_finding_reply_must_use_the_day_in_the_asking_record(self):
        self.pending(prepared=TODAY - timedelta(days=3))
        asked = TODAY - timedelta(days=2)
        findings.record_asked(self.root, self.draft, str(asked), today=TODAY)
        other = TODAY - timedelta(days=1)
        self.refused(f"--asked-on {other} is not the day in the asking record ({asked})", self.reply, "declined",
                     on=TODAY, asked=other, text=None)
        self.assertEqual(self.reply("declined", on=TODAY, asked=asked, text=None)["status"], "declined")
        self.assertEqual(self.stored()["agency_response"]["asked_on"], str(asked))

    def test_cli_records_the_day_and_publish_then_names_it(self):
        self.pending()
        done = self.cli("finding", "reply-asked", self.draft, "--directory", self.root, "--asked-on", str(TODAY))
        self.assertEqual(json.loads(done.stdout.splitlines()[0])["asked_on"], str(TODAY))
        self.assertIn("Nothing was sent, and the reviewed draft is unchanged.", done.stdout)
        refused = self.cli("finding", "publish", self.draft, "--directory", self.root, "--approved-by",
                           "Pat Organizer", returncode=1)
        self.assertIn(f"the asking record says you asked on {TODAY}", refused.stderr)
        self.assertFalse(self.published().exists())


if __name__ == "__main__":
    unittest.main()
