"""Dry-run an attended gate at a chosen time. Not a receipt, not authorization.

Evaluates the same spec and inputs as `golive_gate` with `--at` as the clock.
It never writes a file. Stdout starts with the DRY RUN line; with --json the
JSON object follows on the next lines, wrapped as {"dry_run": true, ...} so it
is not a gate receipt and `golive_record gate` refuses it.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from . import golive_gate

BANNER = "DRY RUN - not a receipt, not authorization"
# Fields that name an input file; evidence and prior_gate checks also read the
# directory input called "evidence" (see golive_gate._evidence/_prior_gate).
_NAME_FIELDS = ("input", "engine_input")


class _Refusal(ValueError):
    """Bad invocation or bad input: one message, exit 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Refusal(message)


def required_inputs(spec):
    """Names of the inputs a valid spec may consult (no files are read)."""
    names = set()
    for check in spec["checks"]:
        names.update(check[field] for field in _NAME_FIELDS if field in check)
        names.update(check.get("candidate_inputs", ()))
        if check["type"] in ("evidence", "prior_gate"):
            names.add("evidence")
    return names


def _parse_at(text):
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        at = None
    if at is None or at.utcoffset() is None:
        raise _Refusal("--at must be an ISO 8601 time with a UTC offset, e.g. 2026-11-26T10:05:00-08:00")
    return at


def _read_spec(path):
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw, object_pairs_hook=golive_gate._unique_object)
    except OSError:
        raise _Refusal("cannot read spec: file unreadable") from None
    except (ValueError, RecursionError):
        raise _Refusal("cannot read spec: not UTF-8 JSON without duplicate fields") from None


def _arguments(argv):
    parser = _Parser(prog="golive_gate_dryrun", description=__doc__, allow_abbrev=False)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--at", required=True, metavar="ISO_TIME", help="aware time to evaluate at")
    parser.add_argument("--json", action="store_true", help="print the wrapped JSON instead of a check list")
    args = parser.parse_args(argv)
    inputs = {}
    for pair in args.input:
        name, _, path = pair.partition("=")
        if not golive_gate._text(name) or not path or name in inputs:
            raise _Refusal("--input requires a distinct NAME=PATH for each input")
        inputs[name] = path
    return args, inputs, _parse_at(args.at)


def _evaluate(args, inputs, at):
    spec = _read_spec(args.spec)
    try:
        golive_gate.load_spec(spec)
    except ValueError as exc:
        raise _Refusal(f"invalid spec: {exc}") from None
    missing = sorted(required_inputs(spec) - inputs.keys())
    if missing:
        raise _Refusal("missing --input NAME=PATH for: " + ", ".join(missing))
    try:
        return golive_gate.evaluate(spec, inputs, at)
    except (ValueError, OverflowError) as exc:
        raise _Refusal(str(exc)) from None


def main(argv=None):
    try:
        args, inputs, at = _arguments(argv)
        receipt = _evaluate(args, inputs, at)
    except _Refusal as exc:
        print(f"golive_gate_dryrun: {exc}", file=sys.stderr)
        return 2
    print(BANNER)
    if args.json:
        print(json.dumps({"dry_run": True, "notice": BANNER, "evaluation": receipt}, indent=2))
    else:
        print(f'would-be {receipt["verdict"]} as of {receipt["evaluated_at"]}')
        for check in receipt["checks"]:
            print(f'{check["type"]} {json.dumps(check["label"])}: {check["state"]} {check["code"]}')
    return 0 if receipt["verdict"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
