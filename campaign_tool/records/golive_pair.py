"""Check operator-captured wrapper and LIVE plan bytes without executing either."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import NoReturn, TypedDict

_MAX_BYTES = 1024 * 1024
_ENGINE_ID = r"engine-[0-9a-f]{7,40}"
_ENGINE_REF = re.compile(rb"(?<![A-Za-z0-9_-])" + _ENGINE_ID.encode("ascii") + rb"(?![A-Za-z0-9_-])")


class PairResult(TypedDict):
    wrapper_sha256: str
    live_sha256: str
    engines: list[str]
    consistent: bool


class ExpectedPair(TypedDict):
    wrapper_sha256: str
    live_sha256: str
    engine: str


def _check_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
        raise ValueError("capture must be a regular, non-symlink file of at most 1 MiB")


def _read_capture(path: Path) -> bytes:
    # Avoid opening special files; protect the final open if the path changes
    # after lstat. NONBLOCK prevents a swapped-in FIFO from blocking the open.
    _check_file(path.lstat())
    with open(path, "rb", opener=_open_capture) as stream:
        _check_file(os.fstat(stream.fileno()))
        data = stream.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError("capture exceeds 1 MiB")
    return data


def _open_capture(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)


def inspect(wrapper_path: str | os.PathLike[str], live_path: str | os.PathLike[str]) -> PairResult:
    """Hash exact bytes and report literal engine names, not inferred SHA aliases."""
    wrapper = _read_capture(Path(wrapper_path))
    live = _read_capture(Path(live_path))
    engines = sorted({ref.decode("ascii") for data in (wrapper, live) for ref in _ENGINE_REF.findall(data)})
    return {
        "wrapper_sha256": hashlib.sha256(wrapper).hexdigest(),
        "live_sha256": hashlib.sha256(live).hexdigest(),
        "engines": engines,
        "consistent": len(engines) == 1,
    }


def compare(result: PairResult, expected: ExpectedPair) -> tuple[bool, list[str]]:
    """Check the byte pins and the single engine pin independently."""
    codes = []
    if result["wrapper_sha256"] != expected["wrapper_sha256"]:
        codes.append("PAIR_WRAPPER_DRIFT")
    if result["live_sha256"] != expected["live_sha256"]:
        codes.append("PAIR_LIVE_DRIFT")
    if not result["consistent"]:
        codes.append("PAIR_ENGINE_MIXED")
    elif result["engines"] != [expected["engine"]]:
        codes.append("PAIR_ENGINE_DRIFT")
    return (False, codes) if codes else (True, ["PAIR_MATCH"])


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse errors can echo arbitrary argument values and file paths.
        raise ValueError("invalid arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="golive_pair", description=__doc__, allow_abbrev=False)
    parser.add_argument("--wrapper", required=True)
    parser.add_argument("--live", required=True)
    parser.add_argument("--expect-wrapper", metavar="SHA256")
    parser.add_argument("--expect-live", metavar="SHA256")
    parser.add_argument("--expect-engine", metavar="engine-HEX")
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        pinned = any(pin is not None for pin in (args.expect_wrapper, args.expect_live, args.expect_engine))
        if pinned and not (
            re.fullmatch(r"[0-9a-f]{64}", args.expect_wrapper or "")
            and re.fullmatch(r"[0-9a-f]{64}", args.expect_live or "")
            and re.fullmatch(_ENGINE_ID, args.expect_engine or "")
        ):
            raise ValueError("incomplete or malformed pin")
        result = inspect(args.wrapper, args.live)
    except (OSError, ValueError):
        print("PAIR_BAD_INPUT", file=sys.stderr)
        return 2

    if pinned:
        ok, codes = compare(result, {
            "wrapper_sha256": args.expect_wrapper,
            "live_sha256": args.expect_live,
            "engine": args.expect_engine,
        })
    else:
        ok = result["consistent"]
        codes = ["PAIR_MATCH" if ok else "PAIR_ENGINE_MIXED"]
    report = {
        "wrapper_sha256": result["wrapper_sha256"],
        "live_sha256": result["live_sha256"],
        "engines": result["engines"],
        "codes": codes,
    }
    if args.json:
        print(json.dumps(report))
    else:
        for key, value in report.items():
            print(f"{key}: {' '.join(value) if isinstance(value, list) else value}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
