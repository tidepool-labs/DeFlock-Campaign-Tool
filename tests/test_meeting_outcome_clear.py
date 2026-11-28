"""DF1125-06: ``meetings-outcome --clear direction|counts`` removes a mistaken staff direction or public-comment count
group from a recorded outcome by itself, keeping every other stored key; the meeting page, ``today`` and open data then
show the outcome as if the group had never been recorded.

Synthetic content only (temp copies of the fictional example, injected ``today``); nothing is fetched.
Run from the repository root: python3 -B -m unittest tests.test_meeting_outcome_clear -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import today
from campaign_tool.content import COMMENT_KEYS, DIRECTION_KEYS
from campaign_tool.meeting_outcome import MeetingOutcomeError, clear_group
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
MID = "cedar-council-2026-10"  # starts_at 2026-10-14T18:00:00-07:00
TODAY = date(2026, 10, 20)
SOURCE = "https://example.invalid/cedar-council/2026-10-14-minutes"
BASE = {"result": "approved", "on": "2026-10-14", "source_url": SOURCE, "verified_by": "Synthetic Organizer",
        "verified_at": "2026-10-20", "vote": "4-1", "recording_url": "https://example.invalid/cedar-council/video",
        "minutes_url": "https://example.invalid/cedar-council/approved-minutes", "minutes_approved_on": "2026-10-20"}
DIRECTION = {"direction": "Return with a draft usage policy", "return_by": "2026-10-25",
             "direction_source": "https://example.invalid/cedar-council/staff-report"}
COUNTS = {"spoke": 7, "written": 0, "comment_source_url": "https://example.invalid/cedar-council/comments"}


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


class ClearTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-clear-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "synthetic campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meeting = self.root / "content" / "meetings" / "2026-10-cedar-council.json"
        self.original = json.loads(self.meeting.read_text(encoding="utf-8"))

    def set_outcome(self, outcome):
        document = {**self.original, "outcome": outcome} if outcome is not None else self.original
        self.meeting.write_text(json.dumps(document), encoding="utf-8")

    def outcome(self):
        return json.loads(self.meeting.read_text(encoding="utf-8")).get("outcome")

    def files(self):
        return {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def public(self):
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def cli(self, *extra):
        return run("meetings-outcome", "--directory", str(self.root), "--id", MID, *extra)

    def test_each_clear_removes_only_its_group_and_keeps_every_other_key_in_order(self):
        full = {**BASE, **DIRECTION, **COUNTS}
        for group, keys in (("direction", DIRECTION_KEYS), ("counts", COMMENT_KEYS)):
            with self.subTest(group=group):
                self.set_outcome(full)
                returned = clear_group(self.root, MID, group, today=TODAY)
                expected = [(k, v) for k, v in full.items() if k not in keys]
                self.assertEqual(list(self.outcome().items()), expected)
                self.assertEqual(returned, dict(expected))
                stored = json.loads(self.meeting.read_text(encoding="utf-8"))
                self.assertEqual({k: v for k, v in stored.items() if k != "outcome"}, self.original)
        self.set_outcome({**BASE, "written": 3, "comment_source_url": SOURCE})  # a written-only group clears too
        clear_group(self.root, MID, "counts", today=TODAY)
        self.assertEqual(self.outcome(), BASE)

    def test_cli_clear_prints_outcome_and_notice(self):
        self.set_outcome({**BASE, **DIRECTION, **COUNTS})
        done = self.cli("--clear", "direction")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.rstrip().splitlines()
        self.assertEqual(lines[-1], "Cleared direction only. Nothing was published.")
        self.assertEqual(json.loads("\n".join(lines[:-1])), {**BASE, **COUNTS})
        done = self.cli("--clear", "counts")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.rstrip().splitlines()[-1], "Cleared counts only. Nothing was published.")
        self.assertEqual(self.outcome(), BASE)

    def test_refusals_write_nothing(self):
        cases = (
            (None, "direction", "--clear direction: needs a recorded outcome"),
            (BASE, "direction", "--clear direction: nothing to clear; meeting cedar-council-2026-10 has no staff direction recorded"),
            ({**BASE, **DIRECTION}, "counts", "--clear counts: nothing to clear"),
            ({**BASE, **COUNTS}, "direction", "--clear direction: nothing to clear"))
        for outcome, group, needle in cases:
            with self.subTest(group=group, outcome=outcome):
                self.set_outcome(outcome)
                before = self.files()
                with self.assertRaises(MeetingOutcomeError) as caught:
                    clear_group(self.root, MID, group, today=TODAY)
                self.assertIn(needle, str(caught.exception))
                self.assertEqual(self.files(), before)
        self.meeting.write_text(json.dumps({**self.original, "status": "cancelled"}), encoding="utf-8")
        before = self.files()
        with self.assertRaises(MeetingOutcomeError) as caught:
            clear_group(self.root, MID, "direction", today=TODAY)
        self.assertIn("is cancelled", str(caught.exception))
        self.assertEqual(self.files(), before)

    def test_clear_combined_with_any_other_outcome_flag_is_refused(self):
        self.set_outcome({**BASE, **DIRECTION, **COUNTS})
        before = self.files()
        for extra, flag in (
                (("--result", "approved"), "--result"), (("--on", "2026-10-14"), "--on"), (("--source", SOURCE), "--source"),
                (("--verified-by", "X"), "--verified-by"), (("--minutes-url", SOURCE), "--minutes-url"),
                (("--minutes-approved-on", "2026-10-20"), "--minutes-approved-on"), (("--vote", "4-1"), "--vote"),
                (("--votes", "v.csv"), "--votes"), (("--recording-url", SOURCE), "--recording-url"),
                (("--recording-start", "0:10:00"), "--recording-start"), (("--continued-to", "x"), "--continued-to"),
                (("--continued-to-date", "2026-12-01"), "--continued-to-date"), (("--direction", "x"), "--direction"),
                (("--return-by", "2026-11-01"), "--return-by"), (("--direction-source", SOURCE), "--direction-source"),
                (("--spoke", "1"), "--spoke"), (("--written", "1"), "--written"),
                (("--comment-source", SOURCE), "--comment-source"), (("--replace",), "--replace")):
            with self.subTest(flag=flag):
                done = self.cli("--clear", "direction", *extra)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(f"--clear cannot be combined with {flag}; nothing was changed", done.stderr)
                self.assertEqual(self.files(), before)
        done = self.cli("--clear", "votes")  # only the two groups exist
        self.assertEqual(done.returncode, 2)
        self.assertIn("invalid choice", done.stderr)
        self.assertEqual(self.files(), before)

    def test_page_today_and_open_data_match_an_outcome_never_given_the_groups(self):
        self.set_outcome(BASE)
        build_site(self.root, today=TODAY)
        never = self.public()
        never_today = [i for i in today.todo(self.root, TODAY) if i["kind"] == "direction_pending"]
        self.assertEqual(never_today, [])
        shutil.rmtree(self.root / "public")
        self.set_outcome({**BASE, **DIRECTION, **COUNTS})
        build_site(self.root, today=TODAY)
        page = (self.root / "public" / "meetings" / f"{MID}.html").read_text(encoding="utf-8")
        self.assertIn("Staff were directed to", page)  # the groups were shown before the clear
        self.assertIn("7 people spoke", page)
        self.assertEqual(len([i for i in today.todo(self.root, TODAY) if i["kind"] == "direction_pending"]), 1)
        shutil.rmtree(self.root / "public")
        clear_group(self.root, MID, "direction", today=TODAY)
        clear_group(self.root, MID, "counts", today=TODAY)
        self.assertEqual([i for i in today.todo(self.root, TODAY) if i["kind"] == "direction_pending"], [])
        build_site(self.root, today=TODAY)
        self.assertEqual(self.public(), never)  # every built byte, meeting page and data/meetings.json included


if __name__ == "__main__":
    unittest.main()
