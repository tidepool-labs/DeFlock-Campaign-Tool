"""Explain a rehearsal receipt in plain words. Read-only; it performs and authorizes nothing."""

import argparse
import re
import sys
from pathlib import Path

from . import rehearsal_receipt

DRIVER_ERROR = "REHEARSAL_DRIVER_ERROR"
_RERUN = "re-run `records rehearse --stage X` from a git checkout"
# Bound by name, not by unpacking the tuple, so a new receipt code cannot shift these; the test that compares
# CODES with rehearsal_receipt._CODES makes any code without plain-words text fail loudly.
_FAILED, _MANUAL, _EMPTY, _OUTPUT = (rehearsal_receipt.FAILED, rehearsal_receipt.MANUAL, rehearsal_receipt.EMPTY,
                                     rehearsal_receipt.OUTPUT)
_SANDBOX, _ENGINE, _STALE, _MISMATCH = (rehearsal_receipt.SANDBOX, rehearsal_receipt.ENGINE, rehearsal_receipt.STALE,
                                        rehearsal_receipt.MISMATCH)
_INCOMPLETE = rehearsal_receipt.INCOMPLETE

# code -> (plain reason sentence, one offline next action). Neither text may hold
# paths, step names or outputs, and no `next` may name a live action. The reason
# sentences avoid semicolons: the line format splits on "; next: ".
_TEXT = {
    _FAILED: (
        "At least one rehearsal step reported a failure instead of OK, so the run did not pass",
        "inspect the failed step's name in the receipt, fix its cause offline, then " + _RERUN,
    ),
    _MANUAL: (
        "The rehearsal left one or more steps for a person to do by hand, so it was not fully rehearsed",
        "read the receipt's manual_steps list, make the driver cover them with synthetic inputs, "
        "then " + _RERUN,
    ),
    _EMPTY: (
        "The rehearsal recorded no steps, so nothing shows that it ran",
        "check that the driver for this stage records at least one step, then " + _RERUN,
    ),
    _OUTPUT: (
        "A file the rehearsal declared as output was missing, changed or not a plain file when it was checked",
        _RERUN + " so the outputs are hashed again, and compare the declared outputs with the run folder if it repeats",
    ),
    _SANDBOX: (
        "The run folder did not carry the private sandbox marker, so the rehearsal cannot be shown to have run in a sandbox",
        _RERUN + " with a new private --out folder",
    ),
    _ENGINE: (
        "The rehearsal could not tell which engine version it ran against, for example because it ran outside "
        "a git checkout or the checkout had uncommitted changes",
        _RERUN + " with no uncommitted changes",
    ),
    _STALE: (
        "The receipt is older than 14 days or is dated in the future, so it no longer counts",
        "re-run within 14 days of when the receipt is needed: " + _RERUN,
    ),
    _MISMATCH: (
        "The receipt was made against a different engine version than the one being checked",
        _RERUN + " at the engine version being checked",
    ),
    _INCOMPLETE: (
        "The receipt claims a stage but did not come from that stage's own driver or is missing some of the "
        "driver's required steps, so it does not count for that stage",
        "run the stage's own driver so every required step is recorded: " + _RERUN,
    ),
    DRIVER_ERROR: (
        "The rehearsal driver crashed before it could finish its steps",
        "inspect the failed step's name in the receipt (it is the error type), fix the driver offline, "
        "then " + _RERUN,
    ),
}
CODES = tuple(_TEXT)
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


def _shown(code):
    """Echo only code-shaped text, so a bad receipt cannot carry a path into the output."""
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "(not a code)"


def _is_driver_crash(receipt):
    """True for a failed run whose driver crashed: the runner recorded a crash step and the crash code."""
    steps = receipt.get("steps")
    return (receipt.get("result") == "fail"
            and DRIVER_ERROR in (receipt.get("codes") or [])
            and isinstance(steps, list)
            and any(isinstance(step, dict) and step.get("code") == DRIVER_ERROR for step in steps))


def explain(receipt):
    """Return the plain-words lines for a receipt: verdict, one line per reason, then observed refusals."""
    lines = ["QUALIFIED" if receipt.get("qualified") is True else "NOT QUALIFIED"]
    # A crash receipt carries only the generic failed-step code in qualify_codes, so the crash is read from the
    # steps and codes; its specific reason takes the place of the generic failed-step line, never an extra one.
    crashed = _is_driver_crash(receipt)
    for code in receipt.get("qualify_codes") or []:
        if crashed and code == _FAILED:
            code = DRIVER_ERROR
        if isinstance(code, str) and code in _TEXT:
            reason, action = _TEXT[code]
            lines.append(f"reason: {reason}; next: {action}")
        else:
            lines.append(f"unrecognized code {_shown(code)}")
    if receipt.get("result") == "pass":
        # A passing run only holds codes that a negative step was meant to provoke.
        for code in dict.fromkeys(receipt.get("codes") or []):
            lines.append(f"observed (expected refusal): {_shown(code)}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", metavar="RECEIPT", type=Path, help="rehearsal receipt JSON file")
    args = parser.parse_args(argv)
    try:
        if not args.receipt.is_file():
            raise ValueError("not a file")
        receipt = rehearsal_receipt.read(args.receipt)
    except (OSError, ValueError):
        # Never echo the path or any receipt content.
        print("golive_rehearsal_explain: unreadable or invalid rehearsal receipt", file=sys.stderr)
        return 2
    lines = explain(receipt)
    print("\n".join(lines))
    return 0 if receipt["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
