"""Read go-live evidence metadata; validity does not establish acceptance."""

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from campaign_tool import law

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "records-golive-evidence-v1.json"
_MAX_SIZE = 64 * 1024
_MAX_FILES = 5000
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def load_schema(path=None):
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _schema():
    return load_schema()


def _validate(value, schema, where):
    # Keep diagnostics in schema order and do not relay raw values from law's errors.
    if schema["type"] == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{where}: expected object")
        for field in schema["required"]:
            path = f"{where}.{field}"
            if field not in value:
                raise ValueError(f"{path}: required")
            _validate(value[field], schema["properties"][field], path)
        for field in value:
            if field not in schema["properties"]:
                raise ValueError(f"{where}[{ascii(field)}]: unexpected field")
    elif schema["type"] == "array":
        if not isinstance(value, list):
            raise ValueError(f"{where}: expected array")
        limit = schema["maxItems"]
        if len(value) > limit:
            raise ValueError(f"{where}: more than {limit} items")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{where}[{index}]")
    else:
        try:
            law.validate(value, schema)
            if schema.get("format") == "date-time":
                datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, RecursionError):
            raise ValueError(f"{where}: invalid value") from None


def validate(record):
    """Raise ValueError naming the first invalid field, without echoing its value."""
    _validate(record, _schema(), "$")


def _filename(record):
    stamp = record["recorded_at"].replace("-", "").replace(":", "")
    return f"{stamp}-{record['stage']}-{record['kind']}-{record['source']['sha256'][:8]}.json"


def _open_directory(directory):
    # Open one component at a time so even a trailing slash or a symlink in
    # an ancestor cannot cause the kernel to follow a link before O_NOFOLLOW.
    path = Path(directory)
    parts = path.parts[1:] if path.anchor else path.parts
    fd = os.open(path.anchor or ".", _DIR_FLAGS)
    try:
        for part in parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except (OSError, ValueError):
        os.close(fd)
        raise
    return fd


def _names(dir_fd, label):
    names = []
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                names.append(entry.name)
                if len(names) > _MAX_FILES:
                    raise ValueError(f"{label}: more than {_MAX_FILES} JSON files")
    return sorted(names)


class _DuplicateField(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField("duplicate JSON field")
        result[key] = value
    return result


def _check_file(info):
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("file mode is not 0600")
    if info.st_size > _MAX_SIZE:
        raise ValueError("file exceeds 64 KiB")


def _read_at(dir_fd, name):
    props = _schema()["properties"]
    stages = "|".join(props["stage"]["enum"])
    kinds = "|".join(props["kind"]["enum"])
    if not re.fullmatch(rf"[0-9]{{8}}T[0-9]{{6}}Z-(?:{stages})-(?:{kinds})-[0-9a-f]{{8}}\.json", name):
        raise ValueError("invalid evidence filename")
    # Screen out non-regular entries before open, then check the opened inode
    # too. NONBLOCK keeps a concurrent replacement by a FIFO from stalling.
    _check_file(os.stat(name, dir_fd=dir_fd, follow_symlinks=False))
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        _check_file(os.fstat(fd))
        raw = stream.read(_MAX_SIZE + 1)
    if len(raw) > _MAX_SIZE:
        raise ValueError("file exceeds 64 KiB")
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except _DuplicateField:
        raise
    except (ValueError, RecursionError):
        raise ValueError("invalid UTF-8 JSON") from None
    validate(record)
    if name != _filename(record):
        raise ValueError("filename does not match record")
    return record


def _label(name):
    return name if name.isprintable() else ascii(name)


def load(directory):
    """Return records in timestamp/filename order, or raise without returning any."""
    label = _label(str(directory))
    try:
        dir_fd = _open_directory(directory)
    except (OSError, ValueError):
        raise ValueError(f"{label}: unreadable evidence directory") from None
    try:
        names = _names(dir_fd, label)
        records = []
        for name in names:  # Bound filenames already sort by UTC stamp, then name.
            try:
                records.append(_read_at(dir_fd, name))
            except OSError:
                raise ValueError(f"{_label(name)}: unreadable file") from None
            except ValueError as exc:
                raise ValueError(f"{_label(name)}: {exc}") from None
        return records
    except OSError:
        raise ValueError(f"{label}: unreadable evidence directory") from None
    finally:
        os.close(dir_fd)


def latest(records, stage, kind):
    """Return the newest matching record (filename breaks ties), or None."""
    return max((record for record in records if record["stage"] == stage and record["kind"] == kind),
               key=_filename, default=None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="golive_evidence")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        records = load(args.dir)
    except ValueError as exc:
        print(f"golive_evidence: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(records))
    else:
        for record in records:
            print(record["stage"], record["kind"], record["result"], record["recorded_at"],
                  json.dumps(record["source"]["schema"]), ",".join(record["codes"]), sep="\t")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
