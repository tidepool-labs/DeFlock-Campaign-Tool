"""DF1125-04 slice 1: `finding publish` waits for the reply deadline of a pending agency reply request, unless
`--before-reply-deadline REASON` is given and logged. Synthetic data only; nothing is sent and no socket is opened.

Run from the repository root: python3 -B -m unittest tests.test_finding_reply_wait -v
"""
import json
import os
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from tests import test_finding_reply as reply_tests  # noqa: E402  (module import: not re-collected here)

TODAY = date.today()
SOON = TODAY + timedelta(days=5)
SLUG = reply_tests.SLUG
REASON = "The council vote moved up."


def at(day, hour, tz=None):
    """An aware moment on ``day`` at ``hour`` (local time when ``tz`` is None)."""
    moment = datetime(day.year, day.month, day.day, hour, 0, 0)
    return moment.replace(tzinfo=tz) if tz else moment.astimezone()


class ReplyWaitTests(reply_tests.ReplyBase):
    def pending(self, by=SOON):
        self.request(by=by, today=TODAY)
        self.approve()  # after the request: preparing it changes the reviewed content

    def published(self):
        return self.content / "findings" / f"{SLUG}.json"

    def last_log(self):
        return json.loads((self.root / "private" / "publication-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    def test_a_pending_request_holds_publish_until_its_deadline_day(self):
        self.pending()
        for moment in (None, at(SOON - timedelta(days=1), 23)):
            err = self.refused(f"the agency was given until {SOON} to reply", findings.publish, self.root, self.draft,
                               "Pat Organizer", now=moment)
            self.assertIn("no reply is recorded; no asking record exists", err)
            self.assertIn("--before-reply-deadline REASON; nothing was written", err)
        self.assertFalse(self.published().exists())
        findings.publish(self.root, self.draft, "Pat Organizer", now=at(SOON, 9))  # the deadline day itself
        self.assertTrue(self.published().exists())
        entry = self.last_log()
        self.assertEqual(entry["action"], "publish")
        self.assertNotIn("before_reply_deadline", entry)

    def test_a_recorded_reply_lifts_the_wait(self):
        self.request(by=SOON, today=TODAY)
        self.reply("declined", on=TODAY, asked=TODAY, text=None)
        self.approve()
        findings.publish(self.root, self.draft, "Pat Organizer")
        self.assertEqual(json.loads(self.published().read_text())["agency_response"]["status"], "declined")
        self.assertNotIn("before_reply_deadline", self.last_log())

    def test_the_override_publishes_early_and_logs_the_deadline_and_reason(self):
        self.pending()
        result = findings.publish(self.root, self.draft, "Pat Organizer", before_reply_deadline=f"  {REASON}  ")
        self.assertEqual(result["published"], SLUG)
        entry = self.last_log()
        self.assertEqual(entry["before_reply_deadline"], {"reply_by": str(SOON), "reason": REASON})
        self.assertEqual(entry["id"], "syn-pub-1")
        self.assertNotIn(REASON, self.published().read_text(encoding="utf-8"))  # private log only
        self.assertEqual(os.stat(self.root / "private" / "publication-log.jsonl").st_mode & 0o777, 0o600)

    def test_a_bad_reason_or_an_override_with_nothing_waiting_is_refused(self):
        self.pending()
        for reason, needle in (("   ", "--before-reply-deadline must not be empty"),
                               ("x" * 201, "--before-reply-deadline is longer than 200 characters"),
                               ("line\nbreak", "--before-reply-deadline contains control characters")):
            with self.subTest(needle=needle):
                err = self.refused(needle, findings.publish, self.root, self.draft, "Pat Organizer",
                                   before_reply_deadline=reason)
                self.assertNotIn("line", err.replace("--before-reply-deadline", ""))
        self.refused("only for a finding whose agency reply deadline has not come yet", findings.publish, self.root,
                     self.draft, "Pat Organizer", now=at(SOON, 9), before_reply_deadline=REASON)
        self.assertEqual(findings.publish(self.root, self.draft, "Pat Organizer",
                                          before_reply_deadline="x" * 200)["published"], SLUG)

    def test_without_a_request_or_after_the_deadline_publish_is_unchanged(self):
        self.approve()
        self.refused("only for a finding whose agency reply deadline", findings.publish, self.root, self.draft,
                     "Pat Organizer", before_reply_deadline=REASON)
        findings.publish(self.root, self.draft, "Pat Organizer")
        self.assertNotIn("agency_response", json.loads(self.published().read_text()))
        self.assertNotIn("before_reply_deadline", self.last_log())

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_the_publish_day_is_the_earlier_of_the_local_and_utc_day(self):
        self.pending()
        for zone in ("America/Los_Angeles", "Pacific/Kiritimati"):  # behind UTC, then 14 hours ahead of it
            with self.subTest(zone=zone):
                previous = os.environ.get("TZ")
                os.environ["TZ"] = zone
                time.tzset()
                try:
                    behind = zone.startswith("America")
                    # UTC already on the deadline day while the local day is the day before, and the reverse
                    moment = at(SOON, 2, timezone.utc) if behind else at(SOON - timedelta(days=1), 20, timezone.utc)
                    self.assertNotEqual(moment.astimezone().date(), moment.date())
                    self.refused(f"until {SOON} to reply", findings.publish, self.root, self.draft, "Pat Organizer",
                                 now=moment)
                finally:
                    if previous is None:
                        os.environ.pop("TZ", None)
                    else:
                        os.environ["TZ"] = previous
                    time.tzset()

    def test_cli_refuses_then_publishes_with_the_reason_and_says_so(self):
        self.pending()
        base = ("finding", "publish", self.draft, "--directory", self.root, "--approved-by", "Pat Organizer")
        refused = self.cli(*base, returncode=1)
        self.assertIn(f"until {SOON} to reply", refused.stderr)
        self.assertFalse(self.published().exists())
        done = self.cli(*base, "--before-reply-deadline", REASON)
        self.assertEqual(json.loads(done.stdout.splitlines()[0])["published"], SLUG)
        self.assertIn("Published before the agency's reply deadline; the reason is in private/publication-log.jsonl.",
                      done.stdout)
        self.assertNotIn(REASON, done.stdout + done.stderr)
        self.assertEqual(self.last_log()["before_reply_deadline"]["reason"], REASON)


if __name__ == "__main__":
    unittest.main()
