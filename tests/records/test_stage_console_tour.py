"""Stage console tour: which stage 10-14 exhibit a person can reach by links, with swap and unknown-draft controls."""
import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "records" / "stage_console_tour.py"
_spec = importlib.util.spec_from_file_location("stage_console_tour", SCRIPT)
assert _spec is not None and _spec.loader is not None
tour_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tour_mod)

OWN = {"10": "ag-ten", "11": "prop-eleven-0001", "12": "item-twelve-001", "13": "draftthirteen1"}


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))


def fake_project(root, stage, drop=()):
    """One tiny stage project holding only the outputs the tour reads for that stage."""
    project = root / ("p" + stage)
    run = project / "run-outputs" / ("run_" + stage)
    if stage == "10":
        write(run / "comparison.json", {"rows": [{"agency_id": OWN["10"]}]})
        write(run / "agency-factsheets.json", {"facts": [{"quote": "ten  quote\ntext"}]})
    elif stage == "11":
        write(run / "outcomes" / "gap-proposals.json", {"agencies": [{"requests": [{"proposals": [
            {"proposal_id": OWN["11"], "quote": "eleven passage", "question": "eleven question?"}]}]}]})
    elif stage == "12":
        items = [] if "items" in drop else [{"item_id": OWN["12"] + "-long-tail"}]
        write(run / "outcomes" / "received-items.json", {"items": items})
        write(run / "outcomes" / "timeline.json", {"requests": [{"request_id": "REQ-12"}]})
    else:
        write(project / "approvals" / "queue.jsonl", json.dumps({"event": "draft", "id": OWN["13"] + "x" * 40}) + "\n")
        (project / "run-outputs" / "run_13").mkdir(parents=True)
    return project


def projects(root, **kw):
    made = {s: fake_project(root, s, drop=kw.get("drop", ())) for s in ("10", "11", "12", "13")}
    made["14"] = made["13"]
    return made


def site(pages, unlisted=None):
    """A fake console: Home links every page in ``pages``; ``unlisted`` pages answer but nothing links to them."""
    unlisted = unlisted or {}

    def fetch(path):
        if path == "/":
            return 200, "".join('<a href="%s">x</a>' % p for p in pages)
        if path in pages:
            return 200, pages[path]
        if path in unlisted:
            return 200, unlisted[path]
        return 404, "not found"
    return fetch


def stage_sites():
    """Each stage's own console shows only that stage's exhibits (stage 11 has no page for its proposal id)."""
    return {"10": {"/comparison": "<p>ag-ten</p>", "/facts": "<p>ten quote text</p>"},
            "11": {"/documents": "<p>eleven passage</p><p>eleven question?</p>"},
            "12": {"/documents": "<p>item-twelve-001-long-tail</p>", "/timeline": "<p>REQ-12</p>"},
            "13": {"/drafts": "<p>draftthirteen1</p>"}}


class ClassifyTests(unittest.TestCase):
    def exhibit(self, tokens, expect=()):
        return {"label": "x", "tokens": tokens, "expect": expect, "unique": True}

    def test_reached_by_links_present_but_unlinked_and_missing_are_three_different_answers(self):
        # Contract: NAVIGABLE needs a link path from Home. Bug caught: a page that answers for a probed URL no one
        # links to counted as something a person can reach.
        pages = tour_mod.crawl(site({"/a": "<p>seen-id</p>"}, unlisted={"/hidden": "<p>hidden-id</p>"}), wanted=("/hidden",))
        self.assertEqual(tour_mod.classify(self.exhibit(["seen-id"]), pages)["status"], tour_mod.NAVIGABLE)
        self.assertEqual(tour_mod.classify(self.exhibit(["hidden-id"]), pages)["status"], tour_mod.UNLINKED)
        self.assertEqual(tour_mod.classify(self.exhibit(["nowhere-id"]), pages)["status"], tour_mod.MISSING)

    def test_id_only_in_a_link_target_counts_and_script_text_and_error_pages_do_not(self):
        pages = tour_mod.crawl(site({"/a": '<a href="/doc?id=target-id">go</a><script>var s="script-id"</script>'}))
        self.assertEqual(tour_mod.classify(self.exhibit(["target-id"]), pages)["status"], tour_mod.NAVIGABLE)
        self.assertEqual(tour_mod.classify(self.exhibit(["script-id"]), pages)["status"], tour_mod.MISSING)
        self.assertEqual(tour_mod.classify(self.exhibit(["not found"]), pages)["status"], tour_mod.MISSING)

    def test_all_tokens_must_be_on_the_same_page_and_empty_tokens_never_match(self):
        pages = tour_mod.crawl(site({"/a": "<p>one</p>", "/b": "<p>two</p>"}))
        self.assertEqual(tour_mod.classify(self.exhibit(["one", "two"]), pages)["status"], tour_mod.MISSING)
        self.assertEqual(tour_mod.classify(self.exhibit([]), pages)["status"], tour_mod.MISSING)

    def test_receipt_row_keeps_pages_depth_and_a_token_hash_but_no_page_text(self):
        pages = tour_mod.crawl(site({"/a": "<p>seen-id secret page text</p>"}))
        row = tour_mod.classify(self.exhibit(["seen-id"], expect=("/a",)), pages)
        self.assertEqual(row["shown_on"], [{"page": "/a", "link_depth": 1}])
        self.assertTrue(row["expected_page_shows_it"])
        self.assertNotIn("secret page text", json.dumps(row))
        self.assertNotIn("seen-id", json.dumps(row))


class TourTests(unittest.TestCase):
    def run_tour(self, sites, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            made = projects(Path(tmp), **kw)
            return tour_mod.tour(made, {k: site(v) for k, v in sites.items()})

    def sites(self, **per_stage):
        base = stage_sites()
        base.update(per_stage)
        return base

    def status(self, receipt, stage, label_part):
        return [e["status"] for e in receipt["stages"][stage]["exhibits"] if label_part in e["exhibit"]][0]

    def test_unknown_stage_11_proposal_id_stays_missing_and_is_reported_not_failed(self):
        # Contract: a missing view is a finding, never an invented page. Bug caught: the proposal id matching a
        # generic page, or a missing view turning the whole tour into a failure.
        receipt = self.run_tour(self.sites())
        self.assertEqual(self.status(receipt, "11", "proposal id"), tour_mod.MISSING)
        self.assertEqual(self.status(receipt, "13", "draft id"), tour_mod.NAVIGABLE)
        self.assertEqual(self.status(receipt, "14", "draft id"), tour_mod.NAVIGABLE)
        self.assertEqual(receipt["controls"]["own_id_present"], {"11: gap proposal: its own proposal id": "ABSENT"})
        self.assertEqual((receipt["result"], receipt["failed"]), ("PASS", []))
        self.assertTrue(all(r["human_finding"] is None for r in receipt["stages"].values()))

    def test_swapped_run_cannot_pass(self):
        # Bug caught: another stage's console (or the stage 12 run served as stage 10) satisfying this stage.
        receipt = self.run_tour(self.sites(**{"10": {**stage_sites()["10"], "/documents": "<p>item-twelve-001-long-tail</p>"}}))
        control = receipt["controls"]["swap_into_stage_10"]
        self.assertEqual(control["result"], "FAIL")
        self.assertEqual(control["foreign_unique_ids_shown"], [["12", "received item: its own item id"]])
        self.assertEqual((receipt["result"], receipt["failed"]), ("FAIL", ["swap_into_stage_10"]))

    def test_unknown_draft_must_not_open_any_draft(self):
        good = site(stage_sites()["13"])

        def leaking(path):
            return (200, "<p>draftthirteen1</p>") if path.startswith("/review") else good(path)
        with tempfile.TemporaryDirectory() as tmp:
            fetchers = {k: site(v) for k, v in stage_sites().items()}
            fetchers["13"] = leaking
            receipt = tour_mod.tour(projects(Path(tmp)), fetchers)
        self.assertEqual(receipt["controls"]["unknown_draft_review"]["result"], "FAIL")
        self.assertIn("unknown_draft_review", receipt["failed"])

    def test_a_stage_output_with_nothing_to_look_for_is_refused(self):
        # Bug caught: an exhibit with no tokens reading as shown everywhere (or nowhere) without saying so.
        with self.assertRaises(tour_mod.Refused) as ctx:
            self.run_tour(self.sites(), drop=("items",))
        self.assertEqual(str(ctx.exception), "STAGE_OUTPUT_EMPTY")

    def test_receipt_names_the_run_and_the_digest_of_every_source_file(self):
        receipt = self.run_tour(self.sites())
        info = receipt["stages"]["12"]
        self.assertEqual(info["run"], "run_12")
        self.assertEqual([s["file"] for s in info["sources"]], ["outcomes/received-items.json", "outcomes/timeline.json"])
        self.assertTrue(all(len(s["sha256"]) == 64 for s in info["sources"]))
        text = tour_mod.render({**receipt, "engine": "e" * 40, "dirty": False})
        self.assertIn("Human finding: ______", text)
        self.assertNotIn("accepted", text.lower())


class CliTests(unittest.TestCase):
    def refuse(self, argv):
        err = StringIO()
        with redirect_stderr(err):
            code = tour_mod.main(argv)
        return code, err.getvalue()

    def test_refuses_an_existing_output_dir_a_wrong_engine_and_a_missing_stage_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "in"
            base.mkdir()
            out = Path(tmp) / "out"
            out.mkdir()
            self.assertEqual(self.refuse([str(base), "--out", str(out)]),
                             (2, "REFUSED OUTPUT_DIR_NOT_NEW_OR_INSIDE_CHECKOUT\n"))
            new = Path(tmp) / "new"
            self.assertEqual(self.refuse([str(base), "--out", str(new), "--expect-engine", "0" * 40]),
                             (2, "REFUSED ENGINE_MISMATCH\n"))
            self.assertEqual(self.refuse([str(base), "--out", str(new)]), (2, "REFUSED STAGE_PROJECT_NOT_UNIQUE_10\n"))
            self.assertFalse(new.exists())

    def test_refuses_an_output_dir_inside_the_checkout(self):
        self.assertEqual(self.refuse([tempfile.gettempdir(), "--out", str(tour_mod.REPO / "tour-out")]),
                         (2, "REFUSED OUTPUT_DIR_NOT_NEW_OR_INSIDE_CHECKOUT\n"))


class RealConsoleTests(unittest.TestCase):
    def test_a_real_console_shows_the_passage_and_question_but_no_page_for_the_proposal_id(self):
        # Contract: the tour reads what the real admin console serves. Bug caught: a stage 11 proposal id counted as
        # reachable because a short id happens to occur in some link target.
        from tests.records import test_console_documents as tcd
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            tcd.build(project)
            path = project / "run-outputs" / tcd.RUN / "outcomes" / "gap-proposals.json"
            doc = json.loads(path.read_text())
            for n, prop in enumerate(doc["agencies"][0]["requests"][0]["proposals"]):
                prop["proposal_id"] = "proposal-unique-%04d" % n
            path.write_text(json.dumps(doc))
            found, _, run = tour_mod.exhibits("11", project)
            server, handle = tour_mod.serve(project)
            try:
                pages = tour_mod.crawl(tour_mod.make_fetch(server.server_port))
            finally:
                server.shutdown()
                server.server_close()
                handle.cleanup()
        status = {e["label"]: tour_mod.classify(e, pages)["status"] for e in found}
        self.assertEqual(status["gap proposal: its review question"], tour_mod.NAVIGABLE)
        self.assertEqual(status["gap proposal: its own proposal id"], tour_mod.MISSING)
        self.assertEqual(run, tcd.RUN)


if __name__ == "__main__":
    unittest.main()
