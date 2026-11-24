"""Record a folder of rehearsal and gate receipts as go-live evidence in one pass.

Evidence only: a recorded row never performs, authorizes or schedules a step.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import NamedTuple

from . import golive_evidence, golive_gate, golive_record, rehearsal_receipt

MAX_FILES = 500
MAX_BYTES = 1024 * 1024
REHEARSAL, GATE, IGNORED, REFUSED = "rehearsal", "gate", "ignored", "refused"
_KINDS = {rehearsal_receipt.SCHEMA: REHEARSAL, golive_gate.RECEIPT_SCHEMA: GATE}


class Candidate(NamedTuple):
    name: str             # path relative to the receipts root, "/"-separated
    kind: str             # rehearsal, gate, ignored or refused
    message: str = ""     # why a candidate was refused
    digest: str = ""      # sha256 of the bytes that were classified
    stages: tuple = ()    # distinct declared stages of a rehearsal


class Report(NamedTuple):
    recorded: list        # new evidence file names (would-be names on a dry run)
    already_recorded: int
    refused: list         # (name, message)
    ignored: int


def _label(name):
    return name if name.isprintable() else ascii(name)


def _message(exc):
    if isinstance(exc, OSError):
        return exc.strerror or "filesystem refusal"
    if isinstance(exc, ValueError):
        return str(exc) or "invalid receipt"
    return f"unexpected {type(exc).__name__}"


def _listing(dir_fd):
    with os.scandir(dir_fd) as entries:
        return sorted((entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries)


def _examine(dir_fd, rel, name):
    """Classify one *.json entry by its schema; never follow a link or read past the cap."""
    def refuse(message):
        return Candidate(rel, REFUSED, message)

    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return refuse("symlink is not followed")
        if not stat.S_ISREG(info.st_mode):
            return refuse("not a regular file")
        if info.st_size > MAX_BYTES:
            return refuse("receipt exceeds 1 MiB")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return refuse("not a regular file")
            raw = stream.read(MAX_BYTES + 1)
    except OSError as exc:
        return refuse(_message(exc))
    if len(raw) > MAX_BYTES:
        return refuse("receipt exceeds 1 MiB")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=golive_evidence._unique_object)
    except (ValueError, RecursionError):
        return refuse("invalid receipt JSON")
    schema = document.get("schema") if isinstance(document, dict) else None
    kind = _KINDS.get(schema) if isinstance(schema, str) else None
    if kind is None:
        return Candidate(rel, IGNORED)
    stages = ()
    if kind == REHEARSAL:
        declared = document.get("stages")
        if isinstance(declared, list) and declared and all(isinstance(s, str) for s in declared):
            stages = tuple(dict.fromkeys(declared))
        else:
            stages = (None,)  # the rehearsal module's own validation refuses it, with its message
    return Candidate(rel, kind, "", hashlib.sha256(raw).hexdigest(), stages)


def scan(root):
    """List candidates in sorted order: *.json directly in root and in immediate subdirectories.

    Hidden names are skipped and subdirectories are not followed beyond one level. Symlinks,
    oversize, non-regular (including a directory named *.json) and unparseable files come back
    refused (never silently dropped);
    valid JSON with another schema comes back ignored. More than MAX_FILES files raises
    ValueError, so nothing is quietly left unrecorded.
    """
    try:
        root_fd = golive_evidence._open_directory(root)
    except (OSError, ValueError):
        raise ValueError("unusable receipts directory") from None
    found = []

    def take(dir_fd, base, entries):
        for name, is_dir in entries:
            if name.startswith(".") or not name.endswith(".json"):
                continue
            if is_dir:  # a folder named *.json is refused by name; the top level still descends into it
                found.append(Candidate(base + name, REFUSED, "not a regular file"))
            else:
                found.append(_examine(dir_fd, base + name, name))
            if len(found) > MAX_FILES:
                raise ValueError(f"more than {MAX_FILES} receipt files")

    try:
        top = _listing(root_fd)
        take(root_fd, "", top)
        for name, is_dir in top:
            if not is_dir or name.startswith("."):
                continue
            try:
                child = os.open(name, golive_evidence._DIR_FLAGS, dir_fd=root_fd)
            except OSError as exc:
                found.append(Candidate(f"{name}/", REFUSED, _message(exc)))
                continue
            try:
                take(child, f"{name}/", _listing(child))
            finally:
                os.close(child)
    except OSError:
        raise ValueError("unusable receipts directory") from None
    finally:
        os.close(root_fd)
    return sorted(found, key=lambda item: item.name)


def _identity(record):
    return record["source"]["sha256"], record["stage"], record["kind"]


def _entry_count(evidence):
    """Entries of the evidence directory that `append` counts against its capacity."""
    try:
        dir_fd = golive_evidence._open_directory(evidence)
    except (OSError, ValueError):
        raise ValueError("unusable evidence directory") from None
    try:
        with os.scandir(dir_fd) as entries:
            return sum(1 for _ in entries)
    except OSError:
        raise ValueError("unusable evidence directory") from None
    finally:
        os.close(dir_fd)


def _records(candidate, root, now):
    path = Path(root) / candidate.name
    if candidate.kind == REHEARSAL:
        records = [golive_record.from_rehearsal(path, stage, now) for stage in candidate.stages]
    else:
        records = golive_record.from_gate(path, now)
    if any(record["source"]["sha256"] != candidate.digest for record in records):
        raise ValueError("receipt changed while it was being recorded")
    return records


def record_all(receipts, evidence, now, dry_run=False):
    """Record every receipt under `receipts`; one bad file never stops the others."""
    candidates = scan(receipts)
    try:
        seen = {_identity(record) for record in golive_evidence.load(evidence)}
    except ValueError:
        raise ValueError("unusable evidence directory") from None
    # A dry run models publication capacity too: `append` refuses a new row once the directory
    # holds _MAX_FILES entries of any kind (an existing identity is still a duplicate).
    entries = _entry_count(evidence) if dry_run else 0
    recorded, refused, already, ignored = [], [], 0, 0
    for candidate in candidates:
        if candidate.kind == IGNORED:
            ignored += 1
            continue
        if candidate.kind == REFUSED:
            refused.append((candidate.name, candidate.message))
            continue
        try:
            # Validate every row first: a refused receipt writes nothing at all.
            records = _records(candidate, receipts, now)
        except Exception as exc:  # noqa: BLE001 - one bad receipt must not stop the batch
            refused.append((candidate.name, _message(exc)))
            continue
        for record in records:
            if dry_run:
                if _identity(record) in seen:
                    already += 1
                elif entries >= golive_evidence._MAX_FILES:
                    refused.append((candidate.name, f"stage {record['stage']}: evidence directory is full"))
                    break
                else:
                    seen.add(_identity(record))
                    entries += 1
                    recorded.append(golive_evidence._filename(record))
                continue
            try:
                recorded.append(golive_record.append(record, evidence).name)
            except golive_record.EvidenceDuplicate:
                already += 1
            except Exception as exc:  # noqa: BLE001
                # Rows are published one atomic file at a time; a rerun finds the earlier ones.
                refused.append((candidate.name, f"stage {record['stage']}: {_message(exc)}"))
                break
    return Report(recorded, already, refused, ignored)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--receipts", required=True, type=Path, help="folder of receipts")
    parser.add_argument("--evidence", required=True, type=Path, help="existing evidence directory")
    parser.add_argument("--dry-run", action="store_true", help="validate and classify; write nothing")
    args = parser.parse_args(argv)
    try:
        report = record_all(args.receipts, args.evidence, datetime.now(timezone.utc), args.dry_run)
    except ValueError as exc:
        print(f"golive_record_batch: {exc}", file=sys.stderr)
        return 2
    for name in report.recorded:
        print(f"would-record {name}" if args.dry_run else name)
    for name, message in report.refused:
        print(f"golive_record_batch: refused {_label(name)}: {message}", file=sys.stderr)
    counts = f"already_recorded={report.already_recorded} refused={len(report.refused)} ignored={report.ignored}"
    if args.dry_run:
        print(f"dry-run: recorded=0 would_record={len(report.recorded)} {counts}")
    else:
        print(f"recorded={len(report.recorded)} {counts}")
    return int(bool(report.refused))


if __name__ == "__main__":
    raise SystemExit(main())
