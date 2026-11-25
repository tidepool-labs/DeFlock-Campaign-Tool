"""Check that a stage 15 rehearsal receipt shows a passed rollback drill.

Read-only evidence: the drill verdict and the receipt's qualification are reported
separately. Neither authorizes or schedules any live step.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from . import rehearsal_receipt

STAGE = "15"
DRILL = ("switch", "rollback", "verify_rollback")
STAGE_MISMATCH, MISSING, FAILED, ORDER, NOT_FINAL = CODES = (
    "ROLLBACK_STAGE_MISMATCH",
    "ROLLBACK_STEP_MISSING",
    "ROLLBACK_STEP_FAILED",
    "ROLLBACK_ORDER",
    "ROLLBACK_NOT_FINAL",
)


def _drill_codes(receipt):
    names = [step["name"] for step in receipt["steps"]]
    found = [name for name in DRILL if name in names]
    bad = set()
    if STAGE not in receipt["stages"]:
        bad.add(STAGE_MISMATCH)
    if len(found) != len(DRILL):
        bad.add(MISSING)
    if any(step["code"] != "OK" for step in receipt["steps"] if step["name"] in DRILL):
        bad.add(FAILED)
    # Judge the relative order of whichever drill steps exist (names are unique).
    positions = [names.index(name) for name in found]
    if positions != sorted(positions):
        bad.add(ORDER)
    if "verify_rollback" in names and names[-1] != "verify_rollback":
        bad.add(NOT_FINAL)
    return [code for code in CODES if code in bad]


def check(receipt, now):
    """Return the drill and qualification verdicts for a receipt, separately.

    A receipt that fails validation raises ValueError (via rehearsal_receipt.qualify).
    """
    qualified, qualify_codes = rehearsal_receipt.qualify(receipt, now=now)
    drill_codes = _drill_codes(receipt)
    return {"drill": "FAIL" if drill_codes else "PASS", "drill_codes": drill_codes,
            "qualified": qualified, "qualify_codes": qualify_codes}


def _now():
    return datetime.now(timezone.utc)


def _lines(result):
    drill = "ROLLBACK DRILL: " + result["drill"]
    if result["drill_codes"]:
        drill += " (" + ", ".join(result["drill_codes"]) + ")"
    qualified = "QUALIFIED: " + ("yes" if result["qualified"] else "no")
    if result["qualify_codes"]:
        qualified += " (" + ", ".join(result["qualify_codes"]) + ")"
    return drill, qualified


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt")
    parser.add_argument("--json", action="store_true", help="print the verdicts as JSON")
    args = parser.parse_args(argv)
    try:
        result = check(rehearsal_receipt.read(args.receipt), _now())
    except (OSError, ValueError):
        print("unreadable or invalid rehearsal receipt", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(*_lines(result), sep="\n")
    if result["drill"] != "PASS":
        return 3
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
