"""Compare two go-live ledger JSON outputs taken before and after a sitting.

Read-only. A ledger row or a LIVE state is evidence only: this report neither
performs nor authorizes any attended step, and its output needs human review.
"""

import argparse
import json
import os
import stat
import sys
from typing import Any

from . import golive_catalog


# Rank order is the contract: a stage that drops in rank regressed.
RANK = {"BLOCKED": 0, "BUILT": 1, "REHEARSED": 2, "GATED": 3, "LIVE": 4}
MAX_BYTES = 1024 * 1024


class _DuplicateField(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField("duplicate JSON field")
        result[key] = value
    return result


def _no_constant(_name):
    raise ValueError("non-finite JSON number")


def _states(document, label):
    """Return {stage: state} for a document, or raise ValueError.

    Messages never contain document content, only the label and catalogue IDs.
    """
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError(f"{label}: document needs a rows list")
    rows = document["rows"]
    if len(rows) != len(golive_catalog.STAGES):
        raise ValueError(f"{label}: expected {len(golive_catalog.STAGES)} rows")
    states = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{label}: row {index} is not an object")
        stage, state = row.get("stage"), row.get("state")
        if not isinstance(stage, str) or stage not in golive_catalog.STAGES:
            raise ValueError(f"{label}: row {index} has an unknown stage")
        if stage in states:
            raise ValueError(f"{label}: stage {stage} appears twice")
        if not isinstance(state, str) or state not in RANK:
            raise ValueError(f"{label}: row {index} has an invalid state")
        states[stage] = state
    return states


def _live(states):
    return sum(golive_catalog.STAGES[stage].pipeline and state == "LIVE"
               for stage, state in states.items())


def diff(before, after) -> dict[str, Any]:
    """Return changed stages in catalogue order plus live counts and regressions."""
    old = _states(before, "before")
    new = _states(after, "after")
    changes = []
    for stage in golive_catalog.STAGES:
        if old[stage] == new[stage]:
            continue
        move = "ADVANCED" if RANK[new[stage]] > RANK[old[stage]] else "REGRESSED"
        changes.append(dict(stage=stage, before=old[stage], after=new[stage], move=move))
    return dict(changes=changes, live_before=_live(old), live_after=_live(new),
                regressed=[change["stage"] for change in changes if change["move"] == "REGRESSED"])


def parse_expected(text):
    """Parse a comma list of catalogue stage IDs; empty or unknown IDs raise ValueError."""
    stages = []
    for item in text.split(","):
        item = item.strip()
        if item not in golive_catalog.STAGES:
            raise ValueError("--expect-live needs known catalogue stage IDs")
        if item not in stages:
            stages.append(item)
    return stages


def not_live(after, expected):
    """Return expected stages that are not LIVE in the after document."""
    states = _states(after, "after")
    return [stage for stage in expected if states[stage] != "LIVE"]


def _check_file(info):
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if info.st_size > MAX_BYTES:
        raise ValueError("file exceeds 1 MiB")


def load(path, label):
    """Read one regular non-symlink JSON file; strict JSON, duplicate fields refused."""
    try:
        _check_file(os.stat(path, follow_symlinks=False))
        # NONBLOCK stops a FIFO swapped in after the stat from stalling the open.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            _check_file(os.fstat(fd))
            raw = stream.read(MAX_BYTES + 1)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None
    except OSError:
        raise ValueError(f"{label}: cannot read file") from None
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{label}: file exceeds 1 MiB")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_no_constant)
    except _DuplicateField:
        raise ValueError(f"{label}: duplicate JSON field") from None
    except (ValueError, RecursionError):
        raise ValueError(f"{label}: invalid UTF-8 JSON") from None


def _fail(message):
    print(f"golive_diff: {message}", file=sys.stderr)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--expect-live", help="comma list of stage IDs that must be LIVE after")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = parse_expected(args.expect_live) if args.expect_live is not None else []
    except ValueError as exc:
        return _fail(str(exc))
    try:
        before = load(args.before, "before")
        after = load(args.after, "after")
        result = diff(before, after)
        missing = not_live(after, expected)
    except ValueError as exc:
        return _fail(str(exc))
    ok = not result["regressed"] and not missing
    if args.json:
        print(json.dumps(dict(result, expect_live=expected, not_live=missing, ok=ok), indent=2))
        return 0 if ok else 1
    for change in result["changes"]:
        print(f"stage {change['stage']}: {change['before']} -> {change['after']} {change['move']}")
    advanced = len(result["changes"]) - len(result["regressed"])
    print(f"{len(result['changes'])} changed ({advanced} advanced, {len(result['regressed'])} regressed); "
          f"pipeline live {result['live_before']} -> {result['live_after']}")
    if result["regressed"]:
        print("REGRESSED: " + ", ".join(result["regressed"]))
    if missing:
        print("NOT LIVE: " + ", ".join(missing))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
