"""Check a gate spec's named inputs before the attended gate runs.

Read-only: paths are inspected with lstat only. File contents are never opened
or hashed, evaluate is never called and nothing is written. A READY report only
says every name the spec asks for is mapped to a path of the right kind; it is
not a gate GO and never authorizes a step.
"""

import argparse
import difflib
import json
import os
from pathlib import Path
import stat

from . import golive_gate

FILE, DIRECTORY = "file", "directory"
MAPPED_OK, NOT_MAPPED, MISSING_PATH = "MAPPED_OK", "NOT_MAPPED", "MISSING_PATH"
WRONG_KIND, SYMLINK, TOO_LARGE, UNUSED = "WRONG_KIND", "SYMLINK", "TOO_LARGE", "UNUSED"
LEDGER_INPUT = "evidence"  # the directory input _evidence and _prior_gate look up


def _check_inputs(check):
    """Return (name, kind) pairs the evaluator would look up for one check."""
    names = []
    if check["type"] in ("evidence", "prior_gate"):
        names.append((LEDGER_INPUT, DIRECTORY))
    if "input" in check:
        names.append((check["input"], FILE))
    if "engine_input" in check:
        names.append((check["engine_input"], FILE))
    names.extend((name, FILE) for name in check.get("candidate_inputs", ()))
    return list(dict.fromkeys(names))


def required_inputs(spec):
    """Validate via golive_gate.load_spec; return {name: {kind, used_by}} in spec order."""
    spec = golive_gate.load_spec(spec)
    found = {}
    for check in spec["checks"]:
        for name, kind in _check_inputs(check):
            entry = found.setdefault(name, {"kind": kind, "used_by": []})
            if entry["kind"] != kind:
                raise ValueError(f"input {name!r} is asked for as both a file and a directory")
            entry["used_by"].append(check["label"])
    return found


def _inspect(path, kind):
    """Classify one mapped path with lstat on every component; never open it."""
    info = None
    try:
        parts = Path(os.fspath(path)).absolute().parts
        for depth in range(1, len(parts) + 1):
            info = os.lstat(os.path.join(*parts[:depth]))
            # golive_gate and golive_evidence refuse a link anywhere on the path.
            if stat.S_ISLNK(info.st_mode):
                return SYMLINK
    except (OSError, ValueError, TypeError):
        return MISSING_PATH
    if info is None:
        return MISSING_PATH
    if kind == DIRECTORY:
        return MAPPED_OK if stat.S_ISDIR(info.st_mode) else WRONG_KIND
    if not stat.S_ISREG(info.st_mode):
        return WRONG_KIND
    return TOO_LARGE if info.st_size > golive_gate.MAX_INPUT else MAPPED_OK


def check(spec, inputs):
    """Return {name: report}: required names in spec order, then UNUSED mapped names.

    Required entries carry state, kind and used_by. UNUSED entries carry state and
    hint (the closest required name by difflib, or None).
    """
    required = required_inputs(spec)
    report = {}
    for name, need in required.items():
        path = inputs.get(name)
        state = NOT_MAPPED if path is None else _inspect(path, need["kind"])
        report[name] = {"state": state, "kind": need["kind"], "used_by": list(need["used_by"])}
    for name in sorted(set(inputs) - set(required), key=str):
        close = difflib.get_close_matches(str(name), list(required), n=1)
        report[name] = {"state": UNUSED, "hint": close[0] if close else None}
    return report


def ready(report):
    """True only when every required input is MAPPED_OK and nothing is UNUSED."""
    return all(item["state"] == MAPPED_OK for item in report.values())


def _line(name, item):
    if item["state"] == UNUSED:
        hint = f" (closest required name: {item['hint']})" if item["hint"] else ""
        return f"{name} unused: {UNUSED}{hint}"
    used = ", ".join(json.dumps(label) for label in item["used_by"])
    return f"{name} {item['kind']}: {item['state']} (used by {used})"


def _scrub(message, paths):
    """Drop any supplied path from a diagnostic; longest first so a parent never half-hides a child."""
    for path in sorted({str(p) for p in paths if p}, key=len, reverse=True):
        message = message.replace(path, "<path>")
    return message


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--json", action="store_true", help="print a JSON report rather than one line per name")
    args = parser.parse_args(argv)
    inputs = {}
    try:
        for pair in args.input:
            name, _, path = pair.partition("=")
            if not golive_gate._text(name) or not path or name in inputs:
                raise ValueError("--input requires a distinct NAME=PATH for each input")
            inputs[name] = path
        spec = json.loads(args.spec.read_text(encoding="utf-8"), object_pairs_hook=golive_gate._unique_object)
        report = check(spec, inputs)
    except OSError as exc:
        # str(OSError) embeds the filename; keep only the reason so no path reaches stderr.
        parser.error(f"cannot read --spec: {exc.strerror or type(exc).__name__}")
    except (ValueError, RecursionError) as exc:
        parser.error(_scrub(str(exc), [args.spec, *inputs.values(), *(p.partition("=")[2] for p in args.input)]))
    ok = ready(report)
    if args.json:
        print(json.dumps({"gate_id": spec["gate_id"], "ready": ok, "inputs": report}, indent=2))
    else:
        for name, item in report.items():
            print(_line(name, item))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
