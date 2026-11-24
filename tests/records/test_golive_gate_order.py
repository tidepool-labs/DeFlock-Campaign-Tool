"""Synthetic tests for the gate-order report (C1-C4); no attended step is performed."""

import contextlib
import copy
import io
import json
import unittest
from unittest import mock

from campaign_tool.records import (
    golive_gate_order as mod,
    golive_gate_specs_lanes,
    golive_gate_specs_s5,
    golive_gate_specs_s6,
)

REAL_EDGES = [
    ("c6-findings-on", "l17-packets", 14),
    ("s5-engine-switch", "c5-portal-apply", 14),
    ("s5-engine-switch", "s10-console", 14),
    ("s5-engine-switch", "s6a-trust-mount", 14),
    ("s5-engine-switch", "s8-units", 1),
    ("s6a-trust-mount", "c6-findings-on", 14),
    ("s6a-trust-mount", "s6b-decisions", 14),
    ("s10-console", "a4-hostname", 14),
]
REAL_ORDER = [
    "s5-engine-switch", "c5-portal-apply", "s10-console", "a4-hostname", "s5b-backup",
    "s6a-trust-mount", "c6-findings-on", "l17-packets", "s6b-decisions", "s8-units",
]


def gate(gate_id, *priors, stages=("1",)):
    checks = [{"type": "prior_gate", "label": "x", "gate_id": p, "max_age_days": n} for p, n in priors]
    return {"gate_id": gate_id, "stages": list(stages), "checks": checks}


def fake(*gates):
    table = {g["gate_id"]: g for g in gates}
    return mock.patch.object(mod, "registry", lambda: table)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class EdgesTests(unittest.TestCase):
    def test_c1_real_edges_by_value(self):
        # Contract C1: a hard-coded or truncated edge list fails this exact comparison.
        self.assertEqual(mod.edges(), sorted(REAL_EDGES))

    def test_c1_every_pack_prior_check_is_an_edge(self):
        # Contract C1: a pack prior_gate that the walker skips (new pack, new check) fails.
        expected = sorted(
            (c["gate_id"], gid, c["max_age_days"])
            for pack in (golive_gate_specs_s5, golive_gate_specs_s6, golive_gate_specs_lanes)
            for gid, d in pack.GATES.items() for c in d["checks"] if c["type"] == "prior_gate")
        self.assertEqual(mod.edges(), expected)

    def test_c1_edges_follow_the_registry_not_a_list(self):
        # Contract C1: synthetic registry with other IDs and age 3 must be reported as given.
        with fake(gate("b", ("a", 3)), gate("a")):
            self.assertEqual(mod.edges(), [("a", "b", 3)])

    def test_c1_dangling_prior_raises(self):
        # Contract C1: a prior naming an unregistered gate must fail loudly, not be dropped.
        with fake(gate("b", ("ghost", 2))):
            for call in (mod.edges, mod.order):
                with self.assertRaisesRegex(ValueError, "b names unregistered prior gate ghost"):
                    call()
            self.assertEqual(run()[0], 2)

    def test_c1_duplicate_registration_raises(self):
        # Contract C1: the same ID in two packs would make the order ambiguous.
        dup = {"s5-engine-switch": golive_gate_specs_s5.GATES["s5-engine-switch"]}
        with mock.patch.object(golive_gate_specs_lanes, "GATES", dup):
            with self.assertRaisesRegex(ValueError, "more than one pack"):
                mod.registry()

    def test_c1_reports_pack_defaults_not_spec_overrides(self):
        # Contract C1/C3: spec(max_age_days=2) is a per-run override; the report keeps defaults.
        golive_gate_specs_s6.spec("s10-console", max_age_days=2)
        golive_gate_specs_lanes.spec("a4-hostname", max_age_days=2)
        self.assertEqual(mod.edges(), sorted(REAL_EDGES))


class OrderTests(unittest.TestCase):
    def test_c2_real_order_by_value(self):
        # Contract C2: exact deterministic sequence, ties by gate ID.
        self.assertEqual(mod.order(), REAL_ORDER)

    def test_c2_every_gate_once_and_priors_first(self):
        # Contract C2: a gate missing, repeated or placed before its prior fails.
        result = mod.order()
        self.assertEqual(sorted(result), sorted(mod.registry()))
        for prior, gid, _ in mod.edges():
            self.assertLess(result.index(prior), result.index(gid))

    def test_c2_engine_switch_before_s8_units_and_s10_console(self):
        # Contract C2: the relation named by the card, with its stated ages.
        result = mod.order()
        self.assertLess(result.index("s5-engine-switch"), result.index("s8-units"))
        self.assertLess(result.index("s5-engine-switch"), result.index("s10-console"))
        self.assertIn(("s5-engine-switch", "s8-units", 1), mod.edges())
        self.assertIn(("s5-engine-switch", "s10-console", 14), mod.edges())

    def test_c2_ties_by_gate_id_and_deterministic(self):
        # Contract C2: independent gates come out alphabetically whatever the registry order.
        with fake(gate("z"), gate("m"), gate("a", ("z", 1))):
            self.assertEqual(mod.order(), ["m", "z", "a"])
        with fake(gate("a", ("z", 1)), gate("m"), gate("z")):
            self.assertEqual(mod.order(), ["m", "z", "a"])

    def test_c2_diamond_waits_for_both_priors(self):
        # Contract C2: a gate with two priors (and a repeated edge) is not released early.
        with fake(gate("d", ("b", 1), ("c", 1), ("c", 2)), gate("b", ("a", 1)), gate("c", ("a", 1)), gate("a")):
            self.assertEqual(mod.order(), ["a", "b", "c", "d"])

    def test_c2_cycle_raises_naming_only_cycle_gates(self):
        # Contract C2: a cycle must fail loudly; "tail" depends on it but is not named.
        with fake(gate("a", ("c", 1)), gate("b", ("a", 1)), gate("c", ("b", 1)),
                  gate("tail", ("a", 1)), gate("free")):
            with self.assertRaisesRegex(ValueError, "cycle among: a, b, c$"):
                mod.order()
            code, out, err = run()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cycle among: a, b, c", err)

    def test_c2_self_prior_is_a_cycle(self):
        # Contract C2: a gate that requires itself can never pass.
        with fake(gate("a", ("a", 1))):
            with self.assertRaisesRegex(ValueError, "cycle among: a$"):
                mod.order()


class CliTests(unittest.TestCase):
    def test_c3_default_lines(self):
        # Contract C3: one line per gate with position, ID, stages and each prior with its age.
        code, out, err = run()
        lines = out.splitlines()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines[0], "1. s5-engine-switch (stages 15)")
        self.assertIn("10. s8-units (stages 3, 18) after s5-engine-switch (GO at most 1 days old)", lines)
        self.assertIn("4. a4-hostname (stages A4) after s10-console (GO at most 14 days old)", lines)
        self.assertEqual([ln.split(".")[0] for ln in lines[:-1]], [str(i) for i in range(1, 11)])

    def test_c3_ages_are_labelled_as_pack_defaults(self):
        # Contract C3: a reader must not mistake the printed ages for the per-run override.
        for argv in ((), ("--gate", "s8-units")):
            self.assertIn("pack defaults", run(*argv)[1])
        self.assertIn("pack defaults", json.loads(run("--json")[1])["note"])

    def test_c3_gate_prints_full_upstream_chain_only(self):
        # Contract C3: transitive priors included (l17 <- c6 <- s6a <- s5), siblings excluded.
        code, out, _ = run("--gate", "l17-packets")
        ids = [ln.split()[1] for ln in out.splitlines() if ln[0].isdigit()]
        self.assertEqual((code, ids), (0, ["s5-engine-switch", "s6a-trust-mount", "c6-findings-on", "l17-packets"]))
        ids = [ln.split()[1] for ln in run("--gate", "s5-engine-switch")[1].splitlines() if ln[0].isdigit()]
        self.assertEqual(ids, ["s5-engine-switch"])

    def test_c3_unknown_gate_exits_2(self):
        # Contract C3: an unknown ID is an error with nothing on stdout, not an empty success.
        code, out, err = run("--gate", "nope")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown gate nope", err)
        self.assertEqual(run("--json", "--gate", "nope")[0], 2)

    def test_c3_bad_option_exits_2(self):
        # Contract C3: usage errors share the exit code 2.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mod.main(["--bogus"]), 2)

    def test_c3_json_carries_the_same_data(self):
        # Contract C3: --json rows equal the text rows' data, for the whole order and one chain.
        data = json.loads(run("--json")[1])
        self.assertEqual([r["gate_id"] for r in data["gates"]], REAL_ORDER)
        self.assertEqual(data["gates"][-1], {"position": 10, "gate_id": "s8-units", "stages": ["3", "18"],
                                             "after": [{"gate_id": "s5-engine-switch", "max_age_days": 1}]})
        chain = json.loads(run("--json", "--gate", "s8-units")[1])["gates"]
        self.assertEqual([(r["position"], r["gate_id"]) for r in chain], [(1, "s5-engine-switch"), (10, "s8-units")])


class PurityTests(unittest.TestCase):
    def test_c4_read_only_and_leaves_packs_unchanged(self):
        # Contract C4: no file is opened and the pack tables are not mutated by any call.
        packs = (golive_gate_specs_s5, golive_gate_specs_s6, golive_gate_specs_lanes)
        before = [copy.deepcopy(p.GATES) for p in packs]
        with mock.patch("builtins.open", side_effect=AssertionError("file access")):
            mod.edges(), mod.order(), mod.report("a4-hostname"), run(), run("--json")
        self.assertEqual([p.GATES for p in packs], before)

    def test_c4_registry_is_only_the_three_packs(self):
        # Contract C4: the registry is exactly the union of the three imported GATES tables.
        union = {}
        for p in (golive_gate_specs_s5, golive_gate_specs_s6, golive_gate_specs_lanes):
            union.update(p.GATES)
        self.assertEqual(mod.registry(), union)


if __name__ == "__main__":
    unittest.main()
