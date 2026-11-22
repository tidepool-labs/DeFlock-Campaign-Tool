"""Rehearse a stage 15 pair switch and rollback in a private synthetic sandbox."""

import argparse
import hashlib
import json
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from . import golive_pair, preswitch, rehearsal_trust, run_stages

STAGES = ("15",)
TITLE = "Engine switch and rollback"
_PAIR = ("run-records.sh", "LIVE.json")
_ENGINES = {"backup": "engine-1234567", "candidate": "engine-89abcde"}
# Synthetic sandbox backend: read the pair named by argv, write one JSON line, touch nothing else.
_BACKEND = '''"""Inert rehearsal backend; standard library only."""
import hashlib
import json
import sys
from pathlib import Path


def main(argv):
    pair, result = Path(argv[1]), Path(argv[2])
    wrapper = pair.with_name("run-records.sh")
    if not pair.is_file() or not wrapper.is_file():
        return 3
    seen = {"pair_sha256": hashlib.sha256(pair.read_bytes()).hexdigest(),
            "wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest()}
    result.write_text(json.dumps(seen, sort_keys=True) + "\\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
'''


def _load_renderer():
    # Load code at import time, not during run(). No deployment templates or
    # runtime configuration are needed for the pure substitution primitive.
    path = Path(__file__).resolve().parents[2] / "deploy" / "records-runtime" / "render.py"
    spec = spec_from_file_location("_engine_switch_renderer", path)
    if spec is None or spec.loader is None:
        raise ImportError("records runtime renderer unavailable")
    module = module_from_spec(spec)
    search_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = search_path
    return module


_renderer = _load_renderer()


class _Failed(Exception):
    def __init__(self, *codes):
        self.codes = codes


def _save_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _pair_paths(root):
    return tuple(root / name for name in _PAIR)


def _copy_pair(source, destination):
    # Copy bytes without executing or interpreting the wrapper or the plan.
    for src, dst in zip(_pair_paths(source), _pair_paths(destination)):
        dst.write_bytes(src.read_bytes())
    return _pair_paths(destination)


def _render_pairs(sandbox):
    (sandbox / run_stages.SANDBOX_MARKER).write_text('{"sandbox_marker_version":1}\n', encoding="ascii")
    wrapper, live = (sandbox / (name + ".in") for name in _PAIR)
    wrapper.write_text("# Synthetic wrapper for @ENGINE@\n", encoding="ascii")
    live.write_text(
        '{"engine":"@ENGINE@","target_config_version":1,"stages":['
        '{"name":"stub","argv":["python3","-m","campaign_tool.stub_backend"]}]}\n',
        encoding="ascii",
    )
    for label, engine in _ENGINES.items():
        pair = sandbox / label
        pair.mkdir()
        for template, output in zip((wrapper, live), _pair_paths(pair)):
            output.write_text(_renderer.substitute(template.read_text(encoding="ascii"),
                                                   {"ENGINE": engine}, template.name), encoding="ascii")
    current = sandbox / "current"
    current.mkdir()
    _copy_pair(sandbox / "backup", current)
    return (*_pair_paths(sandbox / "backup"), *_pair_paths(sandbox / "candidate"))


def _preswitch(sandbox, candidate):
    stages = preswitch.load_plan(candidate / "LIVE.json")
    engine = candidate / "source"
    source = engine / "campaign_tool" / "stub_backend.py"
    source.parent.mkdir(parents=True)
    source.write_text(_BACKEND, encoding="ascii")
    inventory = _save_json(sandbox / "inventory.json", [])
    report = preswitch.deps(engine, [stage[1] for stage in stages],
                            inventory=json.loads(inventory.read_text(encoding="utf-8")))
    if report["status"] != "PASS":
        raise _Failed("PRESWITCH_FAILED")
    return (_save_json(sandbox / "preswitch.json", report),)


def _pin(pair, engine):
    observed = golive_pair.inspect(*_pair_paths(pair))
    return {"engine": engine, "wrapper_sha256": observed["wrapper_sha256"],
            "live_sha256": observed["live_sha256"]}


def _check_pair(pair, expected):
    observed = golive_pair.inspect(*_pair_paths(pair))
    ok, codes = golive_pair.compare(observed, expected)
    if not ok:
        raise _Failed(*codes)
    return observed


def _switch(sandbox, candidate, current, expected):
    _copy_pair(candidate, current)
    # A receipt preserves the observed switch hashes after current is restored.
    return (_save_json(sandbox / "switch.json", _check_pair(current, expected)),)


def _stub_run(sandbox, current, expected):
    # One subprocess invocation of the sandbox-only stub backend, never through
    # a shell and with an empty environment (no PYTHONPATH). It reads the CURRENT
    # pair named by its argv and writes its own receipt, stub-run.json.
    observed = _check_pair(current, expected)
    backend = sandbox / "candidate" / "source" / "campaign_tool" / "stub_backend.py"
    result = sandbox / "stub-run.json"
    try:
        done = subprocess.run([sys.executable, str(backend), str(current / "LIVE.json"), str(result)],
                              cwd=sandbox, env={}, timeout=30, check=False, shell=False,
                              capture_output=True)
    except subprocess.TimeoutExpired:
        raise _Failed("BACKEND_TIMEOUT")
    if done.returncode != 0:
        raise _Failed("BACKEND_EXIT_NONZERO")
    try:
        seen = json.loads(result.read_text(encoding="ascii"))
        read = (seen["wrapper_sha256"], seen["pair_sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        raise _Failed("BACKEND_RESULT_INVALID")
    # What the backend hashed must be the pair the driver just swapped in.
    if read != (observed["wrapper_sha256"], observed["live_sha256"]):
        raise _Failed("BACKEND_PAIR_MISMATCH")
    return (result,)


def _verify_rollback(current, expected):
    _check_pair(current, expected)
    return _pair_paths(current)


def _empty_sandbox(path):
    rehearsal_trust.require_private(path)
    if any(path.iterdir()):
        raise ValueError("sandbox must be empty")


def run(sandbox: Path):
    """Run all six steps, stopping at the first failed step."""
    sandbox = rehearsal_trust.sandbox_path(sandbox)
    _empty_sandbox(sandbox)
    backup, candidate, current = (sandbox / name for name in ("backup", "candidate", "current"))

    def render_pairs():
        paths = _render_pairs(sandbox)
        # Pin the starting current bytes before switching, not the backup
        # used at rollback time. Candidate bytes get their own independent pin.
        pins["old"] = _pin(current, _ENGINES["backup"])
        pins["candidate"] = _pin(candidate, _ENGINES["candidate"])
        return paths

    pins = {}
    result = {"stages": list(STAGES), "steps": [], "manual_steps": [], "codes": []}
    for name, step in (
        ("render_pairs", render_pairs),
        ("preswitch", lambda: _preswitch(sandbox, candidate)),
        ("switch", lambda: _switch(sandbox, candidate, current, pins["candidate"])),
        ("stub_run", lambda: _stub_run(sandbox, current, pins["candidate"])),
        ("rollback", lambda: _copy_pair(backup, current)),
        ("verify_rollback", lambda: _verify_rollback(current, pins["old"])),
    ):
        outputs, codes = [], []
        try:
            outputs = [{"path": path.relative_to(sandbox).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in step()]
        except _Failed as error:
            codes = list(error.codes)
        except (OSError, ValueError):
            codes = [name.upper() + "_FAILED"]
        result["steps"].append({"name": name, "code": codes[0] if codes else "OK", "outputs": outputs})
        if codes:
            result["codes"] = codes
            if name in ("switch", "stub_run"):
                # A failure after the swap still puts the starting pair back.
                try:
                    _copy_pair(backup, current)
                except OSError:
                    pass
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        sandbox = rehearsal_trust.sandbox_path(args.sandbox)
        sandbox.mkdir(mode=0o700, exist_ok=True)
        _empty_sandbox(sandbox)
    except (OSError, ValueError):
        parser.error("--sandbox must be a new or empty private 0700 directory")
    result = run(sandbox)
    print(json.dumps(result))
    return int(bool(result["codes"] or result["manual_steps"]))


if __name__ == "__main__":
    raise SystemExit(main())
