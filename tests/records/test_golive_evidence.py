"""Offline runtime contracts for the go-live evidence record and reader."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_evidence as evidence

NAME = "20261121T190000Z-17b-gate-01234567.json"


def row(**overrides):
    return {
        "schema": "records-golive-evidence-v1",
        "stage": "17b",
        "kind": "gate",
        "result": "pass",
        "recorded_at": "2026-11-21T19:00:00Z",
        "engine": None,
        "source": {"schema": "synthetic-v1", "sha256": "0123456789abcdef" * 4, "name": "sample.md"},
        "actor": "operator",
        "codes": ["REVIEW_OK", "CHECK_2"],
    } | overrides


class EvidenceCase(unittest.TestCase):
    def refuses(self, action, expected, *args):
        with self.assertRaises(ValueError) as caught:
            action(*args)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(str(caught.exception), expected)


class ValidateTests(EvidenceCase):
    def test_c1_accepts_wire_values(self):
        """C1: all stage IDs (including 17b), enums and boundary values validate."""
        for stage in [*(str(i) for i in range(1, 21)), "17b", *(f"A{i}" for i in range(1, 8))]:
            with self.subTest(stage=stage):
                self.assertIsNone(evidence.validate(row(stage=stage)))
        for kind, actor, result in [("rehearsal", "rehearsal", "fail"), ("live", "pipeline", "pass")]:
            self.assertIsNone(evidence.validate(row(kind=kind, actor=actor, result=result,
                                                   engine="a" * 40, codes=[])))
        self.assertIsNone(evidence.validate(row(recorded_at="2024-02-29T23:59:59Z", codes=["OK"] * 20)))

    def test_c1_exact_object_fields(self):
        """C1: neither object may omit required keys or carry receipt text."""
        self.refuses(evidence.validate, "$: expected object", [])
        self.refuses(evidence.validate, "$.source: expected object", row(source=None))
        for field in row():
            with self.subTest(field=field):
                self.refuses(evidence.validate, f"$.{field}: required", {k: v for k, v in row().items() if k != field})
        for field in row()["source"]:
            with self.subTest(source_field=field):
                source = {k: v for k, v in row()["source"].items() if k != field}
                self.refuses(evidence.validate, f"$.source.{field}: required", row(source=source))
        self.refuses(evidence.validate, "$: unexpected field", row(notes="synthetic body"))
        self.refuses(evidence.validate, "$.source: unexpected field",
                     row(source=row()["source"] | {"notes": "synthetic body"}))

    def test_c1_scalar_refusals(self):
        """C1: strict types, enums, hashes, calendar UTC stamps and basenames."""
        fields = {
            "schema": ["records-golive-evidence-v2", 1],
            "stage": [0, 17, "0", "01", "21", "17B", "A0", "A8"],
            "kind": ["approval", False],
            "result": ["ok", True],
            "recorded_at": ["2026-02-29T19:00:00Z", "2026-04-31T00:00:00Z", "0000-01-01T00:00:00Z",
                            "2026-1-21T19:00:00Z", "2026-11-21T24:00:00Z", "2026-11-21T19:00:60Z",
                            "2026-11-21T19:00:00Z\n", "2026-11-21T19:00:00+00:00", "2026-11-21T19:00:00.0Z", 0],
            "engine": ["A" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n", False],
            "actor": ["reviewer", None],
        }
        for field, bad in fields.items():
            for value in bad:
                with self.subTest(field=field, value=value):
                    self.refuses(evidence.validate, f"$.{field}: invalid value", row(**{field: value}))
        source_fields = {
            "schema": [None],
            "sha256": ["a" * 63, "a" * 65, "A" * 64, "a" * 64 + "\n", None],
            "name": ["", ".", "..", "/sample.md", "nested/sample.md", "nested\\sample.md", "sample\x00.md", []],
        }
        for field, bad in source_fields.items():
            for value in bad:
                with self.subTest(source_field=field, value=value):
                    source = row()["source"] | {field: value}
                    self.refuses(evidence.validate, f"$.source.{field}: invalid value", row(source=source))

    def test_c1_codes_refusals(self):
        """C1: codes are bounded and checked at their exact index."""
        self.refuses(evidence.validate, "$.codes: expected array", row(codes="OK"))
        self.refuses(evidence.validate, "$.codes: more than 20 items", row(codes=["OK"] * 21))
        for code in [False, "", "lower", "9OK", "_OK", "OK_", "OK__NOW", "OK-NOW", "OK\n", "ÉTAT"]:
            with self.subTest(code=code):
                self.refuses(evidence.validate, "$.codes[1]: invalid value", row(codes=["OK", code]))

    def test_c1_first_bad_field(self):
        """C1: first bad field follows schema order, not input key order."""
        record = dict(reversed(list(row(stage="bad", actor="bad").items())))
        self.refuses(evidence.validate, "$.stage: invalid value", record)


class DirectoryTests(EvidenceCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp).resolve()
        self.directory = self.root / "golive-evidence"
        self.directory.mkdir()

    def put(self, name=NAME, record=None, *, raw=None, mode=0o600):
        path = self.directory / name
        path.write_bytes(json.dumps(row() if record is None else record).encode() if raw is None else raw)
        path.chmod(mode)
        return path

    def bad_file(self, name, reason):
        self.refuses(evidence.load, f"{name}: {reason}", self.directory)

    def test_c2_empty_and_unrelated_entries(self):
        """C2: scans only flat .json candidates; doesn't follow notes or descend."""
        self.assertEqual(evidence.load(self.directory), [])
        folder = self.directory / "archive"
        folder.mkdir()
        (folder / NAME).write_text("not consumed", encoding="utf-8")
        (self.directory / "notes.md").symlink_to(self.root / "missing")
        self.assertEqual(evidence.load(self.directory), [])

    def test_c2_orders_by_timestamp_then_name(self):
        """C2: keeps equal-time receipts and ignores directory insertion order."""
        first = row(recorded_at="2026-11-20T19:00:00Z", stage="A7", kind="live")
        tie = row(kind="rehearsal")
        self.put("20261121T190000Z-17b-rehearsal-01234567.json", tie)
        self.put(record=row())
        self.put("20261120T190000Z-A7-live-01234567.json", first)
        self.assertEqual(evidence.load(self.directory), [first, row(), tie])

    def test_c2_bad_directory(self):
        """C2: rejects missing, non-directory and linked directory paths without traversal."""
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        regular = self.root / "regular"
        regular.touch()
        for path in [str(link) + "/", link / self.directory.name, regular, self.root / "absent", "bad\x00path"]:
            with self.subTest(path=path):
                label = str(path) if "\x00" not in str(path) else "'bad\\x00path'"
                self.refuses(evidence.load, f"{label}: unreadable evidence directory", path)

    def test_c2_cannot_list_directory(self):
        """C2: a directory enumeration error becomes a named ValueError."""
        with patch.object(evidence.os, "scandir", side_effect=PermissionError):
            self.refuses(evidence.load, f"{self.directory}: unreadable evidence directory", self.directory)

    def test_c2_nonregular_files(self):
        """C2: does not read a symlink target, subdirectory or FIFO with a valid filename."""
        target = self.root / "target"
        target.write_text(json.dumps(row()), encoding="utf-8")
        target.chmod(0o600)
        for index, make in enumerate([lambda path: path.symlink_to(target), Path.mkdir, os.mkfifo]):
            with self.subTest(index=index):
                directory = self.root / str(index)
                directory.mkdir()
                make(directory / NAME)
                self.refuses(evidence.load, f"{NAME}: not a regular file", directory)

    def test_c2_mode(self):
        """C2: a readable record with non-private permissions is refused."""
        self.put(mode=0o644)
        self.bad_file(NAME, "file mode is not 0600")

    def test_c2_bad_filenames(self):
        """C2: filename syntax is strict, before reading the payload."""
        for name in ["unbound.json", NAME.replace("17b", "17B"), NAME.replace("gate", "approve"),
                     NAME.replace("01234567", "ABCDEF01"), NAME.replace("190000Z", "19000Z")]:
            with self.subTest(name=name):
                path = self.put(name, raw=b"not JSON")
                self.bad_file(name, "invalid evidence filename")
                path.rename(path.with_suffix(".ignored"))

    def test_c2_filename_binding(self):
        """C2: valid-but-different metadata cannot be attributed to the named slot."""
        for changed in [row(stage="17"), row(kind="live"), row(recorded_at="2026-11-21T19:00:01Z"),
                        row(source=row()["source"] | {"sha256": "1" * 64})]:
            with self.subTest(record=changed):
                self.put(record=changed)
                self.bad_file(NAME, "filename does not match record")

    def test_c2_first_bad_file_never_returns_partial(self):
        """C2: a good prefix and reverse-created errors still fail on the earliest bad name."""
        self.put("99999999T999999Z-17b-gate-01234567.json", raw=b"invalid JSON")
        first_bad = "20261121T190001Z-17b-gate-01234567.json"
        self.put(first_bad, row(recorded_at="2026-11-21T19:00:01Z", actor="bad"))
        self.put()
        self.bad_file(first_bad, "$.actor: invalid value")

    def test_c2_rejects_invalid_json(self):
        """C2: malformed JSON, non-UTF-8 and ambiguous repeated JSON keys are not evidence."""
        for raw, reason in [(b"{", "invalid UTF-8 JSON"), (b"\xff", "invalid UTF-8 JSON"),
                            (b"[]", "$: expected object"), (b'{"result":"fail","result":"pass"}', "duplicate JSON field"),
                            (b'{"source":{"name":"one","name":"two"}}', "duplicate JSON field")]:
            with self.subTest(raw=raw):
                self.put(raw=raw)
                self.bad_file(NAME, reason)

    def test_c2_size_boundary(self):
        """C2: 65536 bytes passes; 65537 bytes fails even if valid JSON."""
        raw = json.dumps(row()).encode().ljust(65536, b" ")
        self.put(raw=raw)
        self.assertEqual(evidence.load(self.directory), [row()])
        self.put(raw=raw + b" ")
        self.bad_file(NAME, "file exceeds 64 KiB")

    def test_c2_count_boundary(self):
        """C2: 5000 candidates pass; the next file refuses the entire directory."""
        expected = []
        for index in range(5000):
            digest = f"{index:08x}" + "0" * 56
            record = row(source=row()["source"] | {"sha256": digest})
            self.put(f"20261121T190000Z-17b-gate-{index:08x}.json", record)
            expected.append(record)
        (self.directory / "notes.md").touch()
        self.assertEqual(evidence.load(self.directory), expected)
        self.put("20261121T190001Z-17b-gate-01234567.json", row(recorded_at="2026-11-21T19:00:01Z"))
        self.refuses(evidence.load, f"{self.directory}: more than 5000 JSON files", self.directory)

    def test_c2_rejects_symlink_swap_at_open(self):
        """C2: an entry replaced by a symlink after metadata checks still isn't read."""
        path = self.put()
        open_file = os.open

        def swap(name, flags, *, dir_fd=None):
            if name == NAME:
                target = path.rename(self.root / "moved")
                path.symlink_to(target)
            return open_file(name, flags, dir_fd=dir_fd)

        with patch.object(evidence.os, "open", side_effect=swap):
            self.bad_file(NAME, "unreadable file")

    def test_c2_rechecks_opened_inode(self):
        """C2: a FIFO replacement after precheck does not enter the payload reader."""
        path = self.put()
        open_file = os.open

        def swap(name, flags, *, dir_fd=None):
            if name == NAME:
                # Fail immediately instead of opening a FIFO with blocking flags.
                path.rename(self.root / "moved")
                os.mkfifo(path, mode=0o600)
                self.assertTrue(flags & os.O_NONBLOCK)
            return open_file(name, flags, dir_fd=dir_fd)

        with patch.object(evidence.os, "open", side_effect=swap):
            self.bad_file(NAME, "not a regular file")

    def test_c2_checks_bytes_even_if_file_grows_after_fstat(self):
        """C2: a growing file cannot bypass the byte limit with a stale stat."""
        path = self.put(raw=json.dumps(row()).encode().ljust(65536, b" "))
        fstat = os.fstat

        def grow(fd):
            before = fstat(fd)
            with path.open("ab") as stream:
                stream.write(b" ")
            return before

        with patch.object(evidence.os, "fstat", side_effect=grow):
            self.bad_file(NAME, "file exceeds 64 KiB")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_evidence",
                               "--dir", str(self.directory), *args], check=False, capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parents[2], timeout=10)

    def test_c4_text_and_json_output(self):
        """C4: text projects only the six fields; JSON returns the exact list."""
        record = row(source=row()["source"] | {"schema": "synthetic\nv1"})
        self.put(record=record)
        self.put("20261121T190001Z-17b-gate-01234567.json", row(recorded_at="2026-11-21T19:00:01Z", codes=[]))
        process = self.run_cli()
        self.assertEqual((process.returncode, process.stdout, process.stderr),
                         (0, '17b\tgate\tpass\t2026-11-21T19:00:00Z\t"synthetic\\nv1"\tREVIEW_OK,CHECK_2\n'
                             '17b\tgate\tpass\t2026-11-21T19:00:01Z\t"synthetic-v1"\t\n', ""))
        process = self.run_cli("--json")
        self.assertEqual((process.returncode, json.loads(process.stdout), process.stderr),
                         (0, [record, row(recorded_at="2026-11-21T19:00:01Z", codes=[])], ""))

    def test_c4_success_when_empty(self):
        """C4: an empty store emits no text lines, or exactly an empty JSON list."""
        for args, stdout in [((), ""), (("--json",), "[]\n")]:
            process = self.run_cli(*args)
            self.assertEqual((process.returncode, process.stdout, process.stderr), (0, stdout, ""))

    def test_c4_unknown_field_names_are_not_disclosed(self):
        secret = "synthetic receipt text\nprivate detail"
        for record, location in [(row(**{secret: "value"}), "$"),
                                 (row(source=row()["source"] | {secret: "value"}), "$.source")]:
            with self.subTest(location=location):
                self.put(record=record)
                self.refuses(evidence.validate, f"{location}: unexpected field", record)
                self.bad_file(NAME, f"{location}: unexpected field")
                for args in [(), ("--json",)]:
                    process = self.run_cli(*args)
                    self.assertEqual((process.returncode, process.stdout, process.stderr),
                                     (2, "", f"golive_evidence: {NAME}: {location}: unexpected field\n"))

    def test_c4_refusal(self):
        """C4: a bad file emits only a stderr diagnostic, never an earlier good row."""
        self.put()
        self.put("wrong.json", raw=b"synthetic body")
        for args in [(), ("--json",)]:
            process = self.run_cli(*args)
            self.assertEqual((process.returncode, process.stdout, process.stderr),
                             (2, "", "golive_evidence: wrong.json: invalid evidence filename\n"))


class LatestTests(unittest.TestCase):
    def test_c3_newest_matching_with_tie(self):
        """C3: filter both selectors and take the newest result, even a fail."""
        first = row(recorded_at="2026-11-20T00:00:00Z", source=row()["source"] | {"sha256": "f" * 64})
        loser = row()
        winner = row(result="fail", source=row()["source"] | {"sha256": "a" * 64})
        other = [row(stage="A2", recorded_at="2026-11-22T00:00:00Z"),
                 row(kind="live", recorded_at="2026-11-22T00:00:00Z")]
        self.assertEqual(evidence.latest([first, winner, *other, loser], "17b", "gate"), winner)
        self.assertEqual(evidence.latest([loser, *other, winner, first], "17b", "gate"), winner)

    def test_c3_no_match(self):
        """C3: a different stage/kind or an empty list is not proof."""
        self.assertIsNone(evidence.latest([row(stage="A3"), row(kind="live")], "17b", "gate"))
        self.assertIsNone(evidence.latest([], "17b", "gate"))
