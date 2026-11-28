"""Unsigned offline sitting-manifest composer: joins render, five-driver set, D8 receipt and window; HOLD on any gap."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "records" / "compose_sitting_manifest.py"
_spec = importlib.util.spec_from_file_location("compose_sitting_manifest", SCRIPT)
assert _spec is not None and _spec.loader is not None
csm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(csm)

NOW = datetime(2026, 11, 25, 13, 0, tzinfo=timezone.utc)
WRAPPER = "e" * 64

OLD_PLACE = '''import argparse


def place(project, config, *, factory=None, now=None, only_id=None):
    return []


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-id")
    return parser
'''
NEW_PLACE = '''import argparse


def place(project, config, *, factory=None, now=None, only_id=None, only_revision=None):
    if only_revision is not None and only_id is None:
        raise ValueError("exact_request_invalid")
    return []


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-id")
    parser.add_argument("--revision", help="exact reviewed revision")
    return parser
'''
# Right parameter names and flag, but the revision is never read: the reviewer's no-op case.
NOOP_PLACE = '''import argparse


def place(project, config, *, factory=None, now=None, only_id=None, only_revision=None):
    return []


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-id")
    parser.add_argument("--revision", help="exact reviewed revision")
    return parser
'''


def placement_receipt(engine, **change):
    """A D32 exact-revision placement rehearsal receipt as the offline driver writes it (checks abridged)."""
    checks = [{"name": "both_previews_would_place", "ok": True},
              {"name": "A_exact_refused_revision_not_current", "ok": True, "code": "exact_revision_not_current"},
              {"name": "A_refusal_zero_mutations", "ok": True, "journal": False},
              {"name": "A_refusal_neighbour_B_untouched", "ok": True},
              {"name": "B_placed_once", "ok": True},
              {"name": "B_placement_neighbour_A_untouched", "ok": True},
              {"name": "malformed_ahead_zero_mutations", "ok": True, "code": "exact_revision_not_current"}]
    receipt = {"kind": csm.PLACEMENT_EVIDENCE_KIND, "repo_head": engine, "repo_dirty": False, "mock_only": True,
               "synthetic": True, "result": "PASS", "failed": [], "checks": checks,
               "counts": {"checks": len(checks), "failed": 0, "tests": 2}, "driver_sha256": "a" * 64}
    receipt.update(change)
    return receipt


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout


def packed(value):
    """(object, sha256 of the exact bytes) as the CLI loader would return them."""
    raw = json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
    return json.loads(raw), csm.sha256(raw)


def render(engine):
    return {"candidate_head": engine, "new_verdict": "FREEZABLE", "unexpected": [],
            "contains": {"D17_mail_part_cite": True, "D1_merge_2019": True, "D2b_merge_2022": True},
            "renders": {"with-decisions": {"rc": 0, "wrapper_sha256": WRAPPER},
                        "without-decisions": {"rc": 0, "wrapper_sha256": "f" * 64}}}


def five(engine):
    stages = {}
    for number, stage in enumerate(csm.STAGES):
        driver_digest = ("%x" % (number + 1)) * 64 if stage != "14" else ("%x" % 4) * 64
        receipt_digest = ("%x" % (number + 6)) * 64 if stage != "14" else ("%x" % 9) * 64
        stages[stage] = {"code": "QUALIFIED_SAME_ENGINE", "driver_source_sha256": driver_digest,
                         "driver_tracked_at_head": True, "engine": engine, "modules": [csm.DRIVERS[stage]],
                         "qualified": True, "receipt": "/private/run/%s-receipt.json" % stage,
                         "receipt_sha256": receipt_digest}
    return {"schema": "five-driver-set-v1", "status": "QUALIFIED_SET", "ran": True, "refusals": [],
            "head": engine, "expect_engine": engine, "repo": "/private/clone", "stages": stages}


def d8(engine, run_packed, status=csm.D8_VERIFIED):
    run, run_sha = run_packed
    inputs = {s: {"driver": csm.DRIVERS[s], "driver_sha256": r["driver_source_sha256"],
                  "receipt_sha256": r["receipt_sha256"]} for s, r in run["stages"].items()}
    gate = [{"stage": s, "code": "EVIDENCE_MATCH", "receipt_codes": [], "receipt_sha256": r["receipt_sha256"]}
            for s, r in run["stages"].items()]
    return {"status": status, "main_commit": engine, "t12_set_sha256": run_sha, "d8_head": "d" * 40,
            "sitting": "HOLD",
            "d8": {"reader_head": engine, "t12_set_sha256": run_sha, "stage_inputs": inputs,
                   "cases": {csm.D8_GATE_CASE: {"status": "READY_FOR_REVIEW", "stages": gate},
                             "reviewed_candidate_is_not_main": {"status": "HOLD", "stages": []}}}}


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name) / "engine"
        source = cls.repo / csm.PLACEMENT_SOURCE
        source.parent.mkdir(parents=True)
        git(cls.repo.parent, "init", "-q", str(cls.repo))
        source.write_text(OLD_PLACE)
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "old")
        cls.old = git(cls.repo, "rev-parse", "HEAD").strip()
        source.write_text(NEW_PLACE)
        git(cls.repo, "commit", "-q", "-am", "exact revision")
        cls.engine = git(cls.repo, "rev-parse", "HEAD").strip()
        source.write_text(NOOP_PLACE)
        git(cls.repo, "commit", "-q", "-am", "names and flag only")
        cls.noop = git(cls.repo, "rev-parse", "HEAD").strip()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def inputs(self, engine=None):
        engine = engine or self.engine
        run = packed(five(engine))
        return packed(render(engine)), run, packed(d8(engine, run))

    def window(self, engine=None, start=NOW + timedelta(hours=2), hours=2, wrapper=WRAPPER):
        return packed({"engine": engine or self.engine, "wrapper_sha256": wrapper, "start": start.isoformat(),
                       "end": (start + timedelta(hours=hours)).isoformat()})

    def compose(self, kit, run, report, **kw):
        kw.setdefault("repo", self.repo)
        kw.setdefault("placement", packed(placement_receipt(kit[0].get("candidate_head"))))
        return csm.compose(kit, run, report, now=NOW, **kw)

    def reviewed(self, kit, run, report, window, **change):
        core = self.compose(kit, run, report, window=window)["manifest_sha256"]
        verdict = dict({"verdict": "APPROVE", "reviewer": "independent reviewer", "window_sha256": window[1],
                        "manifest_sha256": core}, **change)
        return packed(verdict)


class ComposeTests(Fixture):
    def test_complete_reviewed_inputs_are_ready_for_a_booking_request_review_only(self):
        kit, run, report = self.inputs()
        window = self.window()
        got = self.compose(kit, run, report, window=window, review=self.reviewed(kit, run, report, window))
        self.assertEqual(got["holds"], [])
        self.assertEqual(got["disposition"], csm.READY)
        self.assertIs(got["unsigned"], True)
        self.assertIs(got["booked"], False)
        self.assertEqual(got["engine"], self.engine)
        self.assertEqual(got["wrapper_sha256"], WRAPPER)
        self.assertEqual(got["capability"]["exact_revision_placement"], "DEMONSTRATED")
        self.assertEqual(got["d8"]["stage_codes"], dict.fromkeys(csm.STAGES, "EVIDENCE_MATCH"))
        self.assertEqual(got["stages"]["11"]["driver"], "rehearse_gap_proposals")
        self.assertEqual(got["inputs"]["five_driver_set_sha256"], run[1])
        self.assertNotIn("/private", json.dumps(got))

    def test_no_window_holds_on_exactly_the_window(self):
        got = self.compose(*self.inputs())
        self.assertEqual(got["disposition"], csm.HOLD)
        self.assertEqual(got["holds"], ["WINDOW_ABSENT", "WINDOW_UNREVIEWED"])
        self.assertEqual(got["window"], {"window": "ABSENT", "review": "ABSENT"})

    def test_window_without_review_is_unreviewed(self):
        got = self.compose(*self.inputs(), window=self.window())
        self.assertEqual(got["holds"], ["WINDOW_UNREVIEWED"])

    def test_review_must_name_this_window_and_this_manifest_and_approve(self):
        kit, run, report = self.inputs()
        window = self.window()
        other_window = self.window(start=NOW + timedelta(hours=3))
        cases = {
            "WINDOW_UNREVIEWED:REVIEW_FOR_OTHER_DIGEST": self.reviewed(kit, run, report, other_window),
            "WINDOW_UNREVIEWED:REVIEW_NOT_APPROVE": self.reviewed(kit, run, report, window, verdict="RETURN"),
            "WINDOW_UNREVIEWED:REVIEWER_UNNAMED": self.reviewed(kit, run, report, window, reviewer=" "),
        }
        for hold, review in cases.items():
            with self.subTest(hold=hold):
                self.assertEqual(self.compose(kit, run, report, window=window, review=review)["holds"], [hold])
        stale = self.reviewed(kit, run, report, window, manifest_sha256="0" * 64)
        self.assertEqual(self.compose(kit, run, report, window=window, review=stale)["holds"],
                         ["WINDOW_UNREVIEWED:REVIEW_FOR_OTHER_DIGEST"])

    def test_a_review_of_an_earlier_manifest_does_not_carry_over_to_changed_evidence(self):
        kit, run, report = self.inputs()
        window = self.window()
        review = self.reviewed(kit, run, report, window)
        changed = copy.deepcopy(report[0])
        changed["d8_head"] = "c" * 40
        got = self.compose(kit, run, packed(changed), window=window, review=review)
        self.assertEqual(got["holds"], ["WINDOW_UNREVIEWED:REVIEW_FOR_OTHER_DIGEST"])

    def test_expired_invalid_or_foreign_window_holds(self):
        kit, run, report = self.inputs()
        cases = {
            "WINDOW_EXPIRED": self.window(start=NOW - timedelta(hours=3)),
            "WINDOW_INVALID": self.window(hours=-1),
            "MIXED_PINS:WINDOW_NOT_KIT": self.window(engine=self.old),
        }
        for hold, window in cases.items():
            with self.subTest(hold=hold):
                got = self.compose(kit, run, report, window=window, review=self.reviewed(kit, run, report, window))
                self.assertEqual(got["holds"], [hold])
        naive = packed({"engine": self.engine, "wrapper_sha256": WRAPPER, "start": "2026-11-26T10:00:00",
                        "end": "2026-11-26T12:00:00"})
        self.assertIn("WINDOW_INVALID", self.compose(kit, run, report, window=naive)["holds"])
        other_wrapper = self.window(wrapper="a" * 64)
        self.assertIn("MIXED_PINS:WINDOW_NOT_KIT", self.compose(kit, run, report, window=other_wrapper)["holds"])

    def test_engine_without_exact_revision_placement_holds(self):
        got = self.compose(*self.inputs(self.old))
        self.assertIn("REVISION_PLACEMENT_MISSING", got["holds"])
        self.assertEqual(got["capability"]["exact_revision_placement"], "MISSING")
        self.assertNotIn("MIXED_PINS", " ".join(got["holds"]))

    def test_unknown_capability_holds(self):
        kit, run, report = self.inputs()
        self.assertIn("CAPABILITY_UNKNOWN:EXACT_REVISION_PLACEMENT", self.compose(kit, run, report, repo=None)["holds"])
        absent = "0123456789" * 4
        got = self.compose(*self.inputs(absent))
        self.assertEqual(got["capability"], {"exact_revision_placement": "UNKNOWN"})

    def test_revision_flag_without_signature_is_not_the_capability(self):
        half = NEW_PLACE.replace(", only_revision=None", "")
        self.assertFalse(csm._has_exact_revision_placement(half))
        self.assertFalse(csm._has_exact_revision_placement(NEW_PLACE.replace('"--revision"', '"--rev"')))
        self.assertFalse(csm._has_exact_revision_placement(NOOP_PLACE))
        self.assertTrue(csm._has_exact_revision_placement(NEW_PLACE))

    def test_names_and_flag_without_revision_filtering_never_pass(self):
        # RETURN t_2983ca66 item 1: a no-op place() with the right names and a --revision flag is not the capability.
        noop = self.compose(*self.inputs(self.noop))
        self.assertEqual(noop["capability"]["exact_revision_placement"], "MISSING")
        self.assertIn("REVISION_PLACEMENT_MISSING", noop["holds"])
        kit, run, report = self.inputs()
        # Static shape alone, with no behavioural evidence, holds.
        bare = self.compose(kit, run, report, placement=None)
        self.assertEqual(bare["capability"]["exact_revision_placement"], "UNDEMONSTRATED")
        self.assertEqual(bare["holds"], ["REVISION_PLACEMENT_UNDEMONSTRATED", "WINDOW_ABSENT", "WINDOW_UNREVIEWED"])
        # What D32 records against an engine that ignores the requested revision: it places the edited revision.
        ignored = placement_receipt(self.engine, result="FAIL", failed=["A_exact_refused_revision_not_current"],
                                    counts={"checks": 7, "failed": 2, "tests": 2})
        ignored["checks"][1].update(ok=False, code=None)
        ignored["checks"][2].update(ok=False)
        got = self.compose(kit, run, report, placement=packed(ignored))
        self.assertEqual(got["capability"]["exact_revision_placement"], "UNDEMONSTRATED")
        self.assertIn("REVISION_PLACEMENT_NOT_DEMONSTRATED", got["holds"])
        self.assertIn("REVISION_PLACEMENT_NOT_DEMONSTRATED:A_EXACT_REFUSED_REVISION_NOT_CURRENT", got["holds"])
        self.assertIn("REVISION_PLACEMENT_NOT_DEMONSTRATED:A_REFUSAL_ZERO_MUTATIONS", got["holds"])
        self.assertEqual(got["disposition"], csm.HOLD)

    def test_placement_evidence_must_be_this_clean_engine_and_complete(self):
        kit, run, report = self.inputs()
        refusal = csm.PLACEMENT_CHECKS
        cases = {
            "MIXED_PINS:PLACEMENT_EVIDENCE_NOT_KIT_ENGINE": [placement_receipt(self.old),
                                                             placement_receipt(self.engine, repo_dirty=True),
                                                             placement_receipt(self.engine, repo_dirty=None)],
            "PLACEMENT_EVIDENCE_WRONG_KIND": [placement_receipt(self.engine, kind="deflock-d26-transition")],
            "REVISION_PLACEMENT_NOT_DEMONSTRATED": [placement_receipt(self.engine, mock_only=False),
                                                    placement_receipt(self.engine, counts={"failed": "0"})],
        }
        missing = placement_receipt(self.engine)
        missing["checks"] = [c for c in missing["checks"] if c["name"] != "B_placement_neighbour_A_untouched"]
        duplicate = placement_receipt(self.engine)
        duplicate["checks"].append(dict(duplicate["checks"][1]))
        wrong_code = placement_receipt(self.engine)
        wrong_code["checks"][1]["code"] = "exact_not_approved"
        cases["REVISION_PLACEMENT_NOT_DEMONSTRATED:B_PLACEMENT_NEIGHBOUR_A_UNTOUCHED"] = [missing]
        cases["REVISION_PLACEMENT_NOT_DEMONSTRATED:A_EXACT_REFUSED_REVISION_NOT_CURRENT"] = [duplicate, wrong_code,
                                                                                         dict(missing, checks="x")]
        self.assertIn("A_exact_refused_revision_not_current", refusal)
        for hold, receipts in cases.items():
            for receipt in receipts:
                with self.subTest(hold=hold):
                    got = self.compose(kit, run, report, placement=packed(receipt))
                    self.assertIn(hold, got["holds"])
                    self.assertEqual(got["capability"]["exact_revision_placement"], "UNDEMONSTRATED")
        good = packed(placement_receipt(self.engine))
        got = self.compose(kit, run, report, placement=good)
        self.assertEqual(got["capability"]["evidence_sha256"], good[1])
        self.assertEqual(got["capability"]["evidence_driver_sha256"], "a" * 64)

    def test_required_kit_changes_are_checked_by_name_not_by_any_true_entry(self):
        # RETURN t_2983ca66 item 2: an unrelated true entry never stands in for the required changes.
        _, run, report = self.inputs()
        unrelated = render(self.engine)
        unrelated["contains"] = {"UNRELATED": True}
        got = self.compose(packed(unrelated), run, report)
        self.assertEqual([h for h in got["holds"] if h.startswith("KIT")],
                         ["KIT_REQUIRED_CHANGE_MISSING:%s" % name for name in sorted(csm.REQUIRED_CHANGES)])
        self.assertEqual(got["kit"]["required_changes"], dict.fromkeys(csm.REQUIRED_CHANGES, False))
        for contains in ({}, None, [], "D17_mail_part_cite"):
            with self.subTest(contains=contains):
                kit = render(self.engine)
                kit["contains"] = contains
                holds = self.compose(packed(kit), run, report)["holds"]
                self.assertEqual(len([h for h in holds if h.startswith("KIT_REQUIRED_CHANGE_MISSING:")]), 3)
        extra = render(self.engine)
        extra["contains"]["UNRELATED"] = True
        self.assertFalse([h for h in self.compose(packed(extra), run, report)["holds"] if h.startswith("KIT")])
        extra["contains"]["UNRELATED"] = False
        self.assertIn("KIT_CHANGE_ENTRY_INVALID", self.compose(packed(extra), run, report)["holds"])
        truthy = render(self.engine)
        truthy["contains"]["D1_merge_2019"] = 1
        self.assertIn("KIT_REQUIRED_CHANGE_MISSING:D1_merge_2019", self.compose(packed(truthy), run, report)["holds"])

    def test_mixed_pins_hold_by_name(self):
        kit, run, report = self.inputs()
        one = copy.deepcopy(run[0])
        one["stages"]["11"]["engine"] = self.old
        one_packed = packed(one)
        got = self.compose(kit, one_packed, packed(d8(self.engine, one_packed)))
        self.assertIn("MIXED_PINS:STAGE_ENGINE_NOT_KIT_ENGINE:11", got["holds"])
        moved = copy.deepcopy(run[0])
        moved["head"] = moved["expect_engine"] = self.old
        moved_packed = packed(moved)
        self.assertIn("MIXED_PINS:SET_ENGINE_NOT_KIT_ENGINE",
                      self.compose(kit, moved_packed, packed(d8(self.engine, moved_packed)))["holds"])
        self.assertIn("MIXED_PINS:D8_ENGINE_NOT_KIT_ENGINE",
                      self.compose(kit, run, packed(d8(self.old, run)))["holds"])
        other_kit = packed(render(self.old))
        holds = self.compose(other_kit, run, report)["holds"]
        self.assertIn("MIXED_PINS:SET_ENGINE_NOT_KIT_ENGINE", holds)
        self.assertIn("MIXED_PINS:D8_ENGINE_NOT_KIT_ENGINE", holds)

    def test_d8_receipt_for_another_set_or_ci_held_holds(self):
        kit, run, report = self.inputs()
        rerun = copy.deepcopy(run[0])
        rerun["note"] = "a later rerun with the same digests"
        self.assertIn("D8_FOR_OTHER_SET", self.compose(kit, packed(rerun), report)["holds"])
        held = packed(d8(self.engine, run, status=csm.D8_CI_HELD))
        self.assertIn("D8_CI_HELD", self.compose(kit, run, held)["holds"])
        other = packed(d8(self.engine, run, status="T12_REFUSED_NO_DRIVERS_RAN"))
        self.assertIn("D8_NOT_VERIFIED", self.compose(kit, run, other)["holds"])

    def test_d8_stage_codes_are_copied_not_recomputed(self):
        kit, run, report = self.inputs()
        failed = copy.deepcopy(report[0])
        failed["d8"]["cases"][csm.D8_GATE_CASE]["stages"][1]["code"] = "EVIDENCE_FAILED"
        failed["d8"]["cases"][csm.D8_GATE_CASE]["status"] = "HOLD"
        got = self.compose(kit, run, packed(failed))
        self.assertEqual(got["d8"]["stage_codes"]["11"], "EVIDENCE_FAILED")
        self.assertIn("D8_STAGE_NOT_MATCH:11:EVIDENCE_FAILED", got["holds"])
        self.assertIn("D8_GATE_NOT_READY", got["holds"])
        missing = copy.deepcopy(report[0])
        del missing["d8"]["cases"][csm.D8_GATE_CASE]["stages"][4]
        self.assertIn("D8_STAGE_NOT_MATCH:14:STAGE_ABSENT", self.compose(kit, run, packed(missing))["holds"])

    def test_d8_digests_must_equal_the_set_digests(self):
        kit, run, report = self.inputs()
        swapped = copy.deepcopy(report[0])
        swapped["d8"]["stage_inputs"]["12"]["driver_sha256"] = "0" * 64
        self.assertEqual([h for h in self.compose(kit, run, packed(swapped))["holds"] if h.startswith("D8")],
                         ["D8_STAGE_INPUT_MISMATCH:12"])
        receipt = copy.deepcopy(report[0])
        receipt["d8"]["cases"][csm.D8_GATE_CASE]["stages"][0]["receipt_sha256"] = "0" * 64
        self.assertIn("D8_STAGE_INPUT_MISMATCH:10", self.compose(kit, run, packed(receipt))["holds"])

    def test_set_defects_hold(self):
        kit, run, _ = self.inputs()
        cases = {
            "STAGE_MISSING:13": lambda s: s["stages"].pop("13"),
            "STAGE_NOT_QUALIFIED:10": lambda s: s["stages"]["10"].update(modules=["rehearse_drafts"]),
            "SET_NOT_QUALIFIED": lambda s: s.update(refusals=["ENGINE_NOT_EXPECTED_COMMIT"]),
            "SET_HAS_UNKNOWN_STAGE": lambda s: s["stages"].update({"15": {}}),
        }
        for hold, change in cases.items():
            with self.subTest(hold=hold):
                broken = copy.deepcopy(run[0])
                change(broken)
                broken_packed = packed(broken)
                got = self.compose(kit, broken_packed, packed(d8(self.engine, run)))
                self.assertIn(hold, got["holds"])
                self.assertEqual(got["disposition"], csm.HOLD)

    def test_kit_defects_hold(self):
        _, run, report = self.inputs()
        cases = {
            "KIT_NOT_FREEZABLE": lambda k: k.update(unexpected=["x"]),
            "KIT_WRAPPER_MISSING": lambda k: k["renders"]["with-decisions"].update(rc=False),
            "KIT_REQUIRED_CHANGE_MISSING:D17_mail_part_cite": lambda k: k["contains"].update(D17_mail_part_cite=False),
            "KIT_REQUIRED_CHANGE_MISSING:D2b_merge_2022": lambda k: k["contains"].pop("D2b_merge_2022"),
            "KIT_ENGINE_INVALID": lambda k: k.update(candidate_head="HEAD"),
        }
        for hold, change in cases.items():
            with self.subTest(hold=hold):
                kit = render(self.engine)
                change(kit)
                self.assertIn(hold, self.compose(packed(kit), run, report)["holds"])

    def test_malformed_shapes_hold_instead_of_crashing(self):
        kit, run, report = self.inputs()
        got = self.compose(packed({"renders": []}), packed({"stages": []}), packed({"d8": {"cases": []}}))
        self.assertEqual(got["disposition"], csm.HOLD)
        self.assertIn("KIT_ENGINE_INVALID", got["holds"])
        odd = copy.deepcopy(report[0])
        odd["d8"]["stage_inputs"]["10"] = ["not", "an", "object"]
        self.assertIn("D8_STAGE_INPUT_MISMATCH:10", self.compose(kit, run, packed(odd))["holds"])

    def test_d8_stage_rows_must_be_unique_expected_strings(self):
        kit, run, report = self.inputs()
        for bad in (["11"], {"stage": "11"}, 11, None, "15"):
            with self.subTest(stage=bad):
                odd = copy.deepcopy(report[0])
                odd["d8"]["cases"][csm.D8_GATE_CASE]["stages"].append(
                    {"stage": bad, "code": "EVIDENCE_MATCH", "receipt_sha256": "0" * 64})
                got = self.compose(kit, run, packed(odd))
                self.assertIn("D8_GATE_UNEXPECTED_ROW", got["holds"])
                self.assertEqual(got["disposition"], csm.HOLD)
        not_a_row = copy.deepcopy(report[0])
        not_a_row["d8"]["cases"][csm.D8_GATE_CASE]["stages"].append(["11"])
        self.assertIn("D8_GATE_UNEXPECTED_ROW", self.compose(kit, run, packed(not_a_row))["holds"])

    def test_a_duplicate_d8_stage_cannot_mask_an_earlier_failure(self):
        kit, run, report = self.inputs()
        window = self.window()
        dup = copy.deepcopy(report[0])
        rows = dup["d8"]["cases"][csm.D8_GATE_CASE]["stages"]
        rows[1] = dict(rows[1], code="EVIDENCE_FAILED")
        rows.append(dict(rows[1], code="EVIDENCE_MATCH"))
        dup_packed = packed(dup)
        got = self.compose(kit, run, dup_packed, window=window,
                           review=self.reviewed(kit, run, dup_packed, window))
        self.assertEqual(got["d8"]["stage_codes"]["11"], "STAGE_DUPLICATE")
        self.assertEqual(got["holds"], ["D8_STAGE_NOT_MATCH:11:STAGE_DUPLICATE"])
        self.assertEqual(got["disposition"], csm.HOLD)

    def test_free_text_planted_in_inputs_is_never_copied_to_the_manifest(self):
        planted = "/home/someone/private/originals/request-17.pdf"
        kit = render(self.engine)
        kit["contains"][planted] = True
        run = five(self.engine)
        run["stages"]["12"]["engine"] = planted
        run["stages"]["13"]["receipt_sha256"] = planted
        run["stages"]["10"]["driver_source_sha256"] = planted
        run_packed = packed(run)
        report = d8(self.engine, run_packed, status=planted)
        report["d8_head"] = planted
        report["d8"]["cases"][csm.D8_GATE_CASE]["status"] = planted
        report["d8"]["cases"][csm.D8_GATE_CASE]["stages"][0]["code"] = planted
        window = packed({"engine": self.engine, "wrapper_sha256": WRAPPER, "start": planted, "end": planted})
        review = packed({"verdict": "APPROVE", "reviewer": planted, "window_sha256": window[1],
                         "manifest_sha256": "0" * 64})
        got = self.compose(packed(kit), run_packed, packed(report), window=window, review=review)
        text = json.dumps(got)
        self.assertNotIn("/home", text)
        self.assertNotIn("private", text)
        self.assertEqual(got["disposition"], csm.HOLD)
        self.assertIn("KIT_CHANGE_ENTRY_INVALID", got["holds"])
        self.assertIn("D8_STAGE_NOT_MATCH:10:MALFORMED", got["holds"])
        self.assertEqual((got["d8"]["status"], got["d8"]["gate_status"], got["d8"]["d8_head"]),
                         ("MALFORMED", "MALFORMED", None))
        self.assertEqual((got["window"]["start"], got["window"]["end"], got["window"]["reviewer"]), (None, None, None))
        self.assertEqual((got["stages"]["12"]["engine"], got["stages"]["13"]["receipt_sha256"],
                          got["stages"]["10"]["driver_sha256"]), (None, None, None))

    def test_window_times_are_written_normalized(self):
        kit, run, report = self.inputs()
        window = packed({"engine": self.engine, "wrapper_sha256": WRAPPER,
                         "start": "2026-11-26T10:00:00-08:00", "end": "2026-11-26T12:00:00-08:00"})
        got = self.compose(kit, run, report, window=window)
        self.assertEqual((got["window"]["start"], got["window"]["end"]),
                         ("2026-11-26T10:00:00-08:00", "2026-11-26T12:00:00-08:00"))

    def test_manifest_digest_does_not_depend_on_the_clock(self):
        kit, run, report = self.inputs()
        first = csm.compose(kit, run, report, repo=self.repo, now=NOW)
        later = csm.compose(kit, run, report, repo=self.repo, now=NOW + timedelta(days=1))
        self.assertEqual(first["manifest_sha256"], later["manifest_sha256"])


class CliTests(Fixture):
    def write(self, directory, name, value):
        path = Path(directory) / name
        path.write_text(json.dumps(value, indent=2, sort_keys=True))
        return path

    def test_cli_writes_once_and_refuses_overwrite_or_bad_input(self):
        with tempfile.TemporaryDirectory() as td:
            run = five(self.engine)
            run_path = self.write(td, "set.json", run)
            run_packed = (run, csm.sha256(run_path.read_bytes()))
            args = ["--kit", str(self.write(td, "render.json", render(self.engine))), "--five-driver-set",
                    str(run_path), "--d8", str(self.write(td, "d8.json", d8(self.engine, run_packed))),
                    "--placement-evidence", str(self.write(td, "d32.json", placement_receipt(self.engine))),
                    "--repo", str(self.repo), "--out", str(Path(td) / "manifest.json")]
            done = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            summary = json.loads(done.stdout)
            self.assertEqual(summary["disposition"], csm.HOLD)
            self.assertEqual(summary["holds"], ["WINDOW_ABSENT", "WINDOW_UNREVIEWED"])
            written = json.loads((Path(td) / "manifest.json").read_text())
            self.assertEqual(written["manifest_sha256"], summary["manifest_sha256"])
            again = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
            self.assertEqual(again.returncode, 2)
            self.assertIn("already exists", again.stderr)
            (Path(td) / "broken.json").write_text("[1, 2]")
            bad = list(args)
            bad[bad.index("--d8") + 1] = str(Path(td) / "broken.json")
            bad[-1] = str(Path(td) / "other.json")
            refused = subprocess.run([sys.executable, str(SCRIPT), *bad], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 2)
            self.assertFalse((Path(td) / "other.json").exists())


if __name__ == "__main__":
    unittest.main()
