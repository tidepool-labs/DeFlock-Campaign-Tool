"""Print the order the attended go-live gates must pass in, derived from the packs.

Every `prior_gate` check in the registered S5, S6 and lane packs is an edge
"the prior gate's GO must be at most N days old". The order and the ages are
read from the pack GATES tables, never listed here. The ages shown are the
packs' DEFAULTS: `golive_gate_specs_s6.spec()` and the lanes `spec()` accept
an operator `max_age_days` that replaces them for one evaluation.

Read-only and offline. A listed order is not a booking, a receipt or an
authorization; it performs and schedules nothing. Exit 0 on success, 2 on error.
"""

import argparse
import heapq
import json
import sys

from . import golive_gate_specs_lanes, golive_gate_specs_s5, golive_gate_specs_s6

_PACKS = (golive_gate_specs_s5, golive_gate_specs_s6, golive_gate_specs_lanes)
AGE_NOTE = "max ages are the pack defaults; spec() may override max_age_days"


def registry():
    """Return {gate_id: spec dict} merged from the three packs (a duplicate ID raises)."""
    merged = {}
    for pack in _PACKS:
        for gate_id, definition in pack.GATES.items():
            if gate_id in merged:
                raise ValueError(f"gate {gate_id} is registered in more than one pack")
            merged[gate_id] = definition
    return merged


def edges():
    """Sorted (prior_gate_id, gate_id, max_age_days) triples from every prior_gate check."""
    gates = registry()
    found = []
    for gate_id, definition in gates.items():
        for check in definition["checks"]:
            if check["type"] != "prior_gate":
                continue
            prior = check["gate_id"]
            if prior not in gates:
                raise ValueError(f"{gate_id} names unregistered prior gate {prior}")
            found.append((prior, gate_id, check["max_age_days"]))
    return sorted(found)


def _cycle_members(ids, triples):
    # Prune gates with no remaining prior or no remaining dependent; what is
    # left lies on a cycle (or between cycles).
    left = set(ids)
    pairs = {(p, g) for p, g, _ in triples}
    while True:
        has_in = {g for p, g in pairs if p in left and g in left}
        has_out = {p for p, g in pairs if p in left and g in left}
        keep = left & has_in & has_out
        if keep == left:
            return sorted(left)
        left = keep


def order():
    """Every registered gate, priors first, ties by gate ID; a cycle raises ValueError."""
    ids = sorted(registry())
    triples = edges()
    waiting = {gate_id: 0 for gate_id in ids}
    later = {gate_id: set() for gate_id in ids}
    for prior, gate_id, _ in triples:
        if gate_id not in later[prior]:
            later[prior].add(gate_id)
            waiting[gate_id] += 1
    ready = [gate_id for gate_id in ids if not waiting[gate_id]]
    heapq.heapify(ready)
    result = []
    while ready:
        gate_id = heapq.heappop(ready)
        result.append(gate_id)
        for nxt in sorted(later[gate_id]):
            waiting[nxt] -= 1
            if not waiting[nxt]:
                heapq.heappush(ready, nxt)
    if len(result) != len(ids):
        members = _cycle_members([g for g in ids if g not in result], triples)
        raise ValueError(f"prior_gate cycle among: {', '.join(members)}")
    return result


def report(gate_id=None):
    """Rows {position, gate_id, stages, after:[{gate_id, max_age_days}]}.

    With gate_id, only that gate and its transitive priors; positions stay those
    of the full order. Unknown gate_id raises KeyError.
    """
    gates = registry()
    if gate_id is not None and gate_id not in gates:
        raise KeyError(gate_id)
    triples = edges()
    wanted = None
    if gate_id is not None:
        wanted, todo = {gate_id}, [gate_id]
        while todo:
            current = todo.pop()
            for prior, gid, _ in triples:
                if gid == current and prior not in wanted:
                    wanted.add(prior)
                    todo.append(prior)
    rows = []
    for position, gid in enumerate(order(), 1):
        if wanted is not None and gid not in wanted:
            continue
        rows.append({"position": position, "gate_id": gid, "stages": list(gates[gid]["stages"]),
                     "after": [{"gate_id": p, "max_age_days": n} for p, g, n in triples if g == gid]})
    return rows


def _line(row):
    text = f"{row['position']}. {row['gate_id']} (stages {', '.join(row['stages'])})"
    for prior in row["after"]:
        text += f" after {prior['gate_id']} (GO at most {prior['max_age_days']} days old)"
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--gate", metavar="ID", help="print only this gate and its upstream chain")
    parser.add_argument("--json", action="store_true", help="print the same data as JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        rows = report(args.gate)
    except KeyError as exc:
        print(f"unknown gate {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"note": AGE_NOTE, "gates": rows}, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(_line(row))
        print(f"note: {AGE_NOTE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
