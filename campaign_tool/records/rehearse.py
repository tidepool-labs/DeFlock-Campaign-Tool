"""Run trusted in-package synthetic rehearsals and retain their evidence."""

import argparse
import importlib
import importlib.util
import json
import pkgutil
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import rehearsal_receipt

_NAME = re.compile(r"rehearse_[a-z][a-z0-9_]*")
_DRIVER_ERROR = "REHEARSAL_DRIVER_ERROR"


def discover():
    """Return validated driver modules in basename order, without running them."""
    directory = Path(__file__).resolve().parent
    drivers = []
    for _, name, _ in sorted(pkgutil.iter_modules([str(directory)]), key=lambda item: item.name):
        if not _NAME.fullmatch(name):
            continue
        source = directory / (name + ".py")
        if source.is_symlink():
            raise ValueError(f"Invalid rehearsal driver {name}: symlinked source")
        if not source.is_file():
            continue
        fullname = f"{__package__}.{name}"
        try:
            spec = importlib.util.find_spec(fullname)
            if spec is None or spec.origin != str(source):
                raise ImportError("driver does not resolve to its source file")
            driver = importlib.import_module(fullname)
        except (Exception, SystemExit) as error:
            raise ValueError(f"Invalid rehearsal driver {name}: {type(error).__name__}") from None
        exports = vars(driver)
        stages = exports.get("STAGES")
        if (not isinstance(stages, tuple) or not stages
                or not all(isinstance(stage, str) and stage for stage in stages)
                or not isinstance(exports.get("TITLE"), str)
                or not callable(exports.get("run"))):
            raise ValueError(f"Invalid rehearsal driver {name}: expected STAGES, TITLE and callable run")
        drivers.append(driver)
    return tuple(drivers)


def _run_driver(driver, out, engine):
    name = driver.__name__.rsplit(".", 1)[-1]
    sandbox = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=out))
    sandbox.chmod(0o700)
    started = datetime.now(timezone.utc)
    try:
        result = driver.run(sandbox)
    except (Exception, SystemExit) as error:
        result = {
            "stages": list(driver.STAGES),
            "steps": [{"name": type(error).__name__, "code": _DRIVER_ERROR, "outputs": []}],
            "manual_steps": [],
            "codes": [_DRIVER_ERROR],
        }
    finished = datetime.now(timezone.utc)
    receipt = rehearsal_receipt.build(name, result, sandbox, started, finished, engine)
    # Keep the receipt with its relative outputs, and avoid collisions between
    # repeats of the same driver that finish in the same second.
    path = rehearsal_receipt.write(receipt, sandbox)
    return receipt, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="parent directory for retained run sandboxes")
    parser.add_argument("--stage", action="append", default=[], help="select drivers by stage ID; repeatable")
    parser.add_argument("--list", action="store_true", help="list selected drivers without running them")
    parser.add_argument("--json", action="store_true", help="print one JSON object per driver")
    args = parser.parse_args(argv)
    try:
        drivers = discover()
    except ValueError as error:
        parser.error(str(error))
    stages = set(args.stage)
    unknown = stages.difference(stage for driver in drivers for stage in driver.STAGES)
    if unknown:
        parser.error("no rehearsal driver for stage(s): " + ", ".join(sorted(unknown)))
    selected = [driver for driver in drivers if not stages or stages.intersection(driver.STAGES)]
    if args.list:
        for driver in selected:
            row = dict(module=driver.__name__.rsplit(".", 1)[-1], stages=list(driver.STAGES), title=driver.TITLE)
            print(json.dumps(row) if args.json else f"{row['module']} {','.join(row['stages'])}: {row['title']}")
        return 0
    if not selected:
        parser.error("no rehearsal drivers found")
    if args.out is None:
        parser.error("--out is required unless --list is used")

    try:
        out = args.out.resolve()
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (OSError, RuntimeError):
        parser.error("--out is not a usable directory")
    engine = rehearsal_receipt.engine_identity()
    all_qualified = True
    for driver in selected:
        try:
            receipt, path = _run_driver(driver, out, engine)
        except OSError:
            print("rehearse: cannot create a sandbox under --out", file=sys.stderr)
            return 2
        all_qualified &= receipt["qualified"]
        if args.json:
            print(json.dumps({**receipt, "receipt": str(path)}))
        else:
            verdict = "QUALIFIED" if receipt["qualified"] else "NOT QUALIFIED"
            codes = ",".join(dict.fromkeys(receipt["codes"] + receipt["qualify_codes"])) or "-"
            print(f"{receipt['module']} {','.join(receipt['stages'])} {receipt['result']} {verdict} {codes} {path}")
    return 0 if all_qualified else 1


if __name__ == "__main__":
    raise SystemExit(main())
