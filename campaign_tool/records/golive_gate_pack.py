"""Write a validated go-live gate spec from the registered gate packs.

One command per gate: `--list` shows the registered gates and their pins;
`--gate ID [--pin NAME=VALUE ...] [--out DIR]` builds the spec through that
pack's own `spec()` and prints it or writes `<gate_id>.spec.json` privately.
A spec file is evidence configuration only; it never performs or authorizes an
attended step, and it never builds or lifts a hold.
"""

import argparse
import json
import math
import os
import re
import sys

from . import golive_gate, golive_gate_specs_lanes, golive_gate_specs_s5, golive_gate_specs_s6

PACKS = (golive_gate_specs_s5, golive_gate_specs_s6, golive_gate_specs_lanes)
NUMERIC_PINS = frozenset({"disk_floor_gib", "max_age_days"})
NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
# Check fields a pin may set; everything else in a spec must equal the pack template.
PINNED_FIELDS = frozenset({"expected", "min", "max_age_days"})
FILE_INPUT_KEYS = ("input", "engine_input")


def registry():
    """Return {gate_id: pack module}; a gate ID defined by two packs is refused."""
    found = {}
    for pack in PACKS:
        for gate_id in pack.GATES:
            if gate_id in found:
                raise ValueError(f"gate {gate_id!r} is defined in more than one pack")
            found[gate_id] = pack
    return found


def _pin_names(pack, template):
    """Return (required, optional) pin keyword names under the pack's convention."""
    short = pack.__name__.rsplit(".", 1)[-1]
    kinds = [check["type"] for check in template["checks"]]
    shas = [check["input"] for check in template["checks"] if check["type"] == "sha256_pin"]
    if short == "golive_gate_specs_s5":
        return sorted(shas), []
    if short == "golive_gate_specs_s6":
        required = [f"{name}_sha256" for name in shas] + (["disk_floor_gib"] if "disk_floor_gib" in kinds else [])
        return sorted(required), ["max_age_days"]
    if short == "golive_gate_specs_lanes":
        return [], ["max_age_days"]
    raise ValueError(f"no pin convention for pack {short!r}")


def describe(gate_id):
    """Return gate facts for the operator; an unknown ID raises KeyError."""
    pack = registry()[gate_id]
    template = pack.GATES[gate_id]
    required, optional = _pin_names(pack, template)
    inputs = set()
    for check in template["checks"]:
        inputs.update(check[key] for key in FILE_INPUT_KEYS if key in check)
        inputs.update(check.get("candidate_inputs", ()))
        if check["type"] in ("evidence", "prior_gate"):
            inputs.add("evidence")
    return {"gate_id": gate_id, "stages": list(template["stages"]), "title": template["title"],
            "required_pins": required, "optional_pins": optional, "inputs": sorted(inputs)}


def _number(name, text):
    if NUMBER.fullmatch(text) is None:
        raise ValueError(f"pin {name} must be a finite non-negative number")
    try:
        value = int(text) if text.isdigit() else float(text)
    except ValueError:
        raise ValueError(f"pin {name} must be a finite non-negative number") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"pin {name} must be a finite non-negative number")
    return value


def parse_pins(items, allowed):
    """Turn NAME=VALUE strings into spec() keywords; refuse duplicate or unknown names."""
    pins = {}
    for item in items:
        name, sep, text = item.partition("=")
        if not sep or not name:
            raise ValueError(f"pin {item!r} must be NAME=VALUE")
        if name in pins:
            raise ValueError(f"duplicate pin {name!r}")
        if name not in allowed:
            raise ValueError(f"unknown pin {name!r}; allowed: {', '.join(sorted(allowed)) or '(none)'}")
        pins[name] = _number(name, text) if name in NUMERIC_PINS else text
    return pins


def _frame(value):
    checks = [{key: item for key, item in check.items() if key not in PINNED_FIELDS}
              for check in value["checks"]]
    return {**{key: item for key, item in value.items() if key != "checks"}, "checks": checks}


def build(gate_id, pin_items=()):
    """Build the spec through the pack's spec(); a pin may only set pinned fields."""
    pack = registry().get(gate_id)
    if pack is None:
        raise ValueError(f"unknown gate {gate_id!r}")
    facts = describe(gate_id)
    pins = parse_pins(pin_items, set(facts["required_pins"]) | set(facts["optional_pins"]))
    missing = [name for name in facts["required_pins"] if name not in pins]
    if missing:
        raise ValueError(f"{gate_id} requires pin: {', '.join(missing)}")
    try:
        built = golive_gate.load_spec(pack.spec(gate_id, **pins))
    except TypeError as exc:
        raise ValueError(str(exc)) from None
    # Defence in depth: no pin may add, drop or reshape a check (e.g. the portal hold release).
    if _frame(built) != _frame(pack.GATES[gate_id]):
        raise ValueError(f"{gate_id} spec differs from the registered gate beyond its pins")
    return built


def write_spec(out_dir, spec):
    """Create <out_dir>/<gate_id>.spec.json mode 0600, no clobber, no symlink followed."""
    target = os.path.join(out_dir, spec["gate_id"] + ".spec.json")
    payload = json.dumps(spec, indent=2) + "\n"
    with golive_gate._parent_fd(target) as (parent, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
        except BaseException:
            os.unlink(name, dir_fd=parent)  # only the file this call just created
            raise
    return target


class _OneLine(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"{self.prog}: error: {' '.join(message.split())}\n")


def _parser():
    parser = _OneLine(description=__doc__, allow_abbrev=False)
    parser.add_argument("--list", action="store_true", help="list the registered gates and required pins")
    parser.add_argument("--gate", help="gate ID to build")
    parser.add_argument("--pin", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--out", help="existing directory for a new <gate_id>.spec.json (mode 0600)")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.list == (args.gate is not None):
        parser.error("exactly one of --list or --gate is required")
    if args.list and (args.pin or args.out is not None):
        parser.error("--list takes no --pin or --out")
    if not args.list and args.out is not None and not args.out:
        parser.error("--out must name an existing directory")
    try:
        if args.list:
            for gate_id in sorted(registry()):
                facts = describe(gate_id)
                print("\t".join((gate_id, ",".join(facts["stages"]), facts["title"],
                                 ",".join(facts["required_pins"]) or "-")))
            return 0
        spec = build(args.gate, args.pin)
        if args.out is None:
            print(json.dumps(spec, indent=2))
        else:
            print(write_spec(args.out, spec))
    except FileExistsError:
        parser.error(f"refusing to overwrite {args.gate}.spec.json in {args.out!r}")
    except (ValueError, OSError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        parser.error(f"cannot build or write the spec: {reason}" if isinstance(exc, OSError) else reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
