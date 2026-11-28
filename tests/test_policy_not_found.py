"""DF1125-02 slice 1: ``content agency-profile policy-not-found`` records that an organizer looked for a posted ALPR
usage policy and did not find one (``profile.policy_search``); the build validates it by field, never beside
``profile.alpr_policy``, and ``agencies.html`` / the agency page show one fixed sentence and a linked "not found" glance
cell. Synthetic agencies in temporary storage only; nothing is fetched.

Run from the repository root: python3 -B -m unittest tests.test_policy_not_found -v
"""
import contextlib
import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from campaign_tool.content import ContentError, load_agencies  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402

TODAY = date.today()
A, B = "ca-alpha-pd", "ca-beta-so"
POLICY = "https://example.invalid/alpha/policy-a.pdf"
SEARCHED = "https://example.invalid/beta/policies"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SENTENCE = "We did not find a posted ALPR usage policy on"
NO_NETWORK = AssertionError("no network in these tests")
MARKER = "fictional-private-marker-7f3a"  # stands for date text that must never reach either output stream
POLICY_IDS = ("authorized_purposes", "authorized_users_training", "security_monitoring", "sharing_restrictions",
              "custodian_title", "accuracy_correction", "retention_destruction")


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def agencies():
    """Alpha has a posted policy and a reading of it; beta has no policy record yet."""
    alpha = {"agency_id": A, "name": "Alpha Police Department (fictional)", "kind": "police", "profile": {
        "status": "Records requested",
        "alpr_policy": {"url": POLICY, "source_url": "https://example.invalid/alpha", "checked_on": ago(40)},
        "policy_check": {"checked_on": ago(30), "policy_url": POLICY,
                         "elements": {key: {"status": "unclear"} for key in POLICY_IDS}}}}
    beta = {"agency_id": B, "name": "Beta Sheriff (fictional)", "kind": "sheriff", "profile": {"status": "Listed"}}
    return [alpha, beta]


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue(), err.getvalue()


class _NoNetwork(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(socket, "socket", side_effect=NO_NETWORK),
                      mock.patch.object(socket, "create_connection", side_effect=NO_NETWORK),
                      mock.patch.object(urllib.request, "urlopen", side_effect=NO_NETWORK)):
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-policy-not-found-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "campaign"


class RecordTests(_NoNetwork):
    def setUp(self):
        super().setUp()
        (self.root / "content").mkdir(parents=True)
        self.target = self.root / "content" / "agencies.json"
        self.target.write_text(json.dumps(agencies(), indent=2) + "\n", encoding="utf-8")

    def profile(self, agency):
        return next(a for a in json.loads(self.target.read_text(encoding="utf-8")) if a["agency_id"] == agency)["profile"]

    def not_found(self, *extra, agency=B, url=SEARCHED, checked_on=None):
        return run("content", "agency-profile", "policy-not-found", "--directory", self.root, "--agency", agency,
                   "--searched-url", url, "--checked-on", checked_on or ago(5), *extra)

    def policy(self, *extra, agency=B, checked_on=None):
        return run("content", "agency-profile", "policy", "--directory", self.root, "--agency", agency, "--url",
                   "https://example.invalid/beta/policy.pdf", "--source-url", SEARCHED, "--checked-on",
                   checked_on or ago(2), *extra)

    def assert_refused(self, result, needle):
        before = self.target.read_bytes()
        code, out, err = result()
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out, "")
        self.assertIn(needle, err)
        self.assertIn("nothing was written", err)
        self.assertEqual(self.target.read_bytes(), before)

    def test_record_and_show(self):
        code, out, err = self.not_found()
        self.assertEqual(code, 0, err)
        self.assertIn(f"Recorded profile.policy_search for {B} (checked_on {ago(5)}).", out)
        self.assertNotIn("Removed", out)
        self.assertEqual(self.profile(B)["policy_search"],
                         {"status": "not_found", "searched_url": SEARCHED, "checked_on": ago(5)})
        load_agencies(json.loads(self.target.read_text(encoding="utf-8")), "content/agencies.json", TODAY)
        # recording it again needs --replace, which records the whole reading again
        self.assert_refused(lambda: self.not_found(checked_on=ago(1)), "pass --replace")
        self.assertEqual(self.not_found("--replace", checked_on=ago(1))[0], 0)
        self.assertEqual(self.profile(B)["policy_search"]["checked_on"], ago(1))

    def test_refusals_write_nothing(self):
        self.assert_refused(lambda: self.not_found(checked_on=(TODAY + timedelta(days=1)).isoformat()),
                            "must not be after today")
        self.assert_refused(lambda: self.not_found(url="http://example.invalid/beta/policies"), "https:// URL")
        self.assert_refused(lambda: self.not_found(url="https://example.invalid/" + "a" * 480), "at most 500")
        self.assert_refused(lambda: self.not_found("--drop-check"), "--drop-check requires --replace")
        self.assertNotIn("policy_search", self.profile(B))

    def test_swap_from_posted_policy(self):
        self.assert_refused(lambda: self.not_found(agency=A), "profile.alpr_policy: ca-alpha-pd already has one")
        self.assert_refused(lambda: self.not_found("--replace", agency=A), "--drop-check")
        code, out, err = self.not_found("--replace", "--drop-check", agency=A)
        self.assertEqual(code, 0, err)
        self.assertIn(f"Removed profile.alpr_policy (checked_on {ago(40)})", out)
        self.assertIn(f"Dropped profile.policy_check (checked_on {ago(30)})", out)
        profile = self.profile(A)
        self.assertNotIn("alpr_policy", profile)
        self.assertNotIn("policy_check", profile)
        self.assertEqual(profile["policy_search"]["status"], "not_found")

    def test_swap_back_to_posted_policy(self):
        self.assertEqual(self.not_found()[0], 0)
        self.assert_refused(self.policy, "profile.policy_search: ca-beta-so has a 'policy not found' reading")
        code, out, err = self.policy("--replace")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Removed profile.policy_search (checked_on {ago(5)})", out)
        profile = self.profile(B)
        self.assertNotIn("policy_search", profile)
        self.assertEqual(profile["alpr_policy"]["url"], "https://example.invalid/beta/policy.pdf")

    def assert_silent(self, code, out, err):
        self.assertNotIn(MARKER, out, code)
        self.assertNotIn(MARKER, err, code)

    def test_an_invalid_checked_on_is_refused_without_repeating_it(self):
        for verb in (self.not_found, self.policy):
            with self.subTest(verb=verb.__name__):
                before = self.target.read_bytes()
                code, out, err = verb(checked_on=MARKER)
                self.assertNotEqual(code, 0, out)
                self.assertEqual(out, "")
                self.assertIn("--checked-on: must be a YYYY-MM-DD calendar day; nothing was written", err)
                self.assert_silent(code, out, err)
                self.assertEqual(self.target.read_bytes(), before)

    def test_removed_records_with_malformed_dates_are_reported_as_unknown(self):
        document = agencies()
        document[0]["profile"]["alpr_policy"]["checked_on"] = MARKER
        document[0]["profile"]["policy_check"]["checked_on"] = MARKER
        self.target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        code, out, err = self.not_found("--replace", "--drop-check", agency=A)
        self.assertEqual(code, 0, err)
        self.assertIn("Removed profile.alpr_policy (checked_on unknown)", out)
        self.assertIn("Dropped profile.policy_check (checked_on unknown)", out)
        self.assert_silent(code, out, err)
        document = agencies()
        document[1]["profile"]["policy_search"] = {"status": "not_found", "searched_url": SEARCHED,
                                                   "checked_on": MARKER}
        self.target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        code, out, err = self.policy("--replace")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed profile.policy_search (checked_on unknown)", out)
        self.assert_silent(code, out, err)
        self.assertNotIn(MARKER, self.target.read_text(encoding="utf-8"))



class LoaderTests(unittest.TestCase):
    def load(self, search, policy=False):
        document = agencies()
        profile = document[1]["profile"] if not policy else document[0]["profile"]
        profile["policy_search"] = search
        return load_agencies(document, "content/agencies.json", TODAY)

    def refused(self, search, needle, policy=False):
        with self.assertRaises(ContentError) as caught:
            self.load(search, policy)
        self.assertIn(needle, str(caught.exception))

    def test_valid_and_by_field_refusals(self):
        good = {"status": "not_found", "searched_url": SEARCHED, "checked_on": ago(3)}
        loaded = {a["agency_id"]: a for a in self.load(dict(good))}
        self.assertEqual(loaded[B]["policy_search"], good)
        self.assertIsNone(loaded[A]["policy_search"])
        self.refused(dict(good), "profile.policy_search: must not be recorded together with profile.alpr_policy",
                     policy=True)
        self.refused(dict(good, status="missing"), "profile.policy_search.status")
        self.refused(dict(good, extra=1), "unknown field(s) ['extra']")
        self.refused({"status": "not_found", "checked_on": ago(3)}, "needs status, searched_url and checked_on")
        self.refused(dict(good, searched_url="ftp://example.invalid/x"), "profile.policy_search.searched_url")
        self.refused(dict(good, checked_on=(TODAY + timedelta(days=1)).isoformat()), "profile.policy_search.checked_on")
        self.refused(dict(good, checked_on="2026-02-30"), "profile.policy_search.checked_on")
        self.refused("not_found", "profile.policy_search: expected an object")


class SiteTests(_NoNetwork):
    def setUp(self):
        super().setUp()
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text(encoding="utf-8"))

    def build(self):
        build_site(self.root)
        public = self.root / "public"
        return {str(p.relative_to(public)): p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}

    def with_search(self, labels=None):
        document = json.loads(json.dumps(self.base))
        target = next(a for a in document if not (a.get("profile") or {}).get("alpr_policy"))
        target.setdefault("profile", {})["policy_search"] = {"status": "not_found", "searched_url": SEARCHED,
                                                            "checked_on": "2025-03-04"}
        self.path.write_text(json.dumps(document), encoding="utf-8")
        if labels is not None:
            (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}),
                                                               encoding="utf-8")
        return target

    def test_pages_show_the_sentence_and_the_linked_cell(self):
        before = self.build()
        self.assertNotIn(SENTENCE.encode(), before["agencies.html"])
        target = self.with_search()
        after = self.build()
        page = after["agencies.html"].decode("utf-8")
        link = f'<a href="{SEARCHED}" rel="noopener noreferrer">'
        sentence = f'<p class="policy-not-found">{SENTENCE} {link}the agency\'s site</a> (checked 2025-03-04).</p>'
        self.assertIn(sentence, page)
        glance = page.split('id="at-a-glance">', 1)[1].split("</section>", 1)[0]
        self.assertIn(f'{link}not found</a> <small>checked <time datetime="2025-03-04">', glance)
        changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        self.assertEqual(target["agency_id"], "ca-cedar-police")  # the example's police agency; its page is cedar.html
        self.assertEqual(changed, ["agencies.html", "agencies/cedar.html"])
        self.assertIn(sentence, after[changed[1]].decode("utf-8"))  # the agency page; open data and the rest unchanged
        for words in ("required", "violation", "illegal", "must"):
            self.assertNotIn(words, sentence)

    def test_labels_override_every_word(self):
        self.with_search({"agency.policy_not_found": "ZZ {site} ZZ {date} ZZ", "agency.policy_not_found_site": "QQ",
                          "agencies.glance_not_found": "XX"})
        page = self.build()["agencies.html"].decode("utf-8")
        self.assertNotIn(SENTENCE, page)
        self.assertIn(f'<p class="policy-not-found">ZZ <a href="{SEARCHED}" rel="noopener noreferrer">QQ</a> ZZ ', page)
        self.assertIn(f'<a href="{SEARCHED}" rel="noopener noreferrer">XX</a>', page)


if __name__ == "__main__":
    unittest.main()
