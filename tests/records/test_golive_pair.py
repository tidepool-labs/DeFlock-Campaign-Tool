import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from campaign_tool.records import golive_pair

_SHORT = "engine-ab12cd3"
_FULL = "engine-ab12cd345678901234567890abcdef1234567890"
_WRAPPER = b"#!/bin/sh\r\nENGINE_DIR='/fixture/engine-ab12cd3'\r\n# wrapper-only marker\r\n"
_LIVE = b'{"steps":["/fixture/engine-ab12cd3/main.py","/fixture/engine-ab12cd3/data"]}\n'
_EXPECTED: golive_pair.ExpectedPair = {
    "wrapper_sha256": hashlib.sha256(_WRAPPER).hexdigest(),
    "live_sha256": hashlib.sha256(_LIVE).hexdigest(),
    "engine": _SHORT,
}


def _result(wrapper, live, engines, consistent):
    return {
        "wrapper_sha256": hashlib.sha256(wrapper).hexdigest(),
        "live_sha256": hashlib.sha256(live).hexdigest(),
        "engines": engines,
        "consistent": consistent,
    }


class PairTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.wrapper = folder / "run-records.sh"
        self.live = folder / "LIVE.json"
        self.wrapper.write_bytes(_WRAPPER)
        self.live.write_bytes(_LIVE)

    def test_inspect_exact_bytes(self):
        """C1: Keep byte-level hashes and deduplicate references across both captures."""
        self.assertEqual(golive_pair.inspect(str(self.wrapper), self.live),
                         _result(_WRAPPER, _LIVE, [_SHORT], True))

    def test_inspect_engine_references(self):
        """C1: Check both captures, retaining full IDs without collapsing prefixes."""
        full = f'{{"path":"/fixture/{_FULL}"}}'.encode()
        short = b'export PYTHONPATH="/fixture/engine-ab12cd3"\n'
        empty = b""
        # Reject a too-short ID, a too-long ID, a non-hex suffix, and a larger label.
        near_matches = (f"engine-ab12cd {_FULL}0 {_SHORT}x x{_SHORT} {_SHORT}-backup\n").encode()
        for label, wrapper, live, engines, consistent in (
            ("full only", empty, full, [_FULL], True),
            ("eight hex digits", empty, b'"/fixture/engine-1234abcd"', ["engine-1234abcd"], True),
            ("wrapper only", short, empty, [_SHORT], True),
            ("mixed and sorted", full, short, [_SHORT, _FULL], False),
            ("no references", near_matches, empty, [], False),
        ):
            with self.subTest(label=label):
                self.wrapper.write_bytes(wrapper)
                self.live.write_bytes(live)
                self.assertEqual(golive_pair.inspect(self.wrapper, self.live),
                                 _result(wrapper, live, engines, consistent))

    def test_inspect_exact_size_limit(self):
        """C1: Accept exactly 1 MiB and scan through the last byte without decoding the file."""
        engine = _SHORT.encode()
        data = b"\xff" * (1024 * 1024 - len(engine)) + engine
        self.live.write_bytes(data)
        self.assertEqual(golive_pair.inspect(self.wrapper, self.live),
                         _result(_WRAPPER, data, [_SHORT], True))

    def test_inspect_refuses_unsafe_files(self):
        """C1: Refuse dangling/valid links, special files, oversize captures and missing files."""
        folder = self.live.parent
        link = folder / "linked-plan"
        link.symlink_to(self.live)
        missing = folder / "missing"
        dangling = folder / "dangling"
        dangling.symlink_to(missing)
        fifo = folder / "fifo"
        os.mkfifo(fifo)
        oversized = folder / "oversized"
        with oversized.open("wb") as stream:
            stream.truncate(1024 * 1024 + 1)
        for path, error in ((link, ValueError), (dangling, ValueError), (folder, ValueError),
                            (fifo, ValueError), (oversized, ValueError), (missing, FileNotFoundError)):
            for pair in ((self.wrapper, path), (path, self.live)):
                with self.subTest(path=path.name, slot=pair.index(path)):
                    with self.assertRaises(error) as caught:
                        golive_pair.inspect(*pair)
                    self.assertIs(type(caught.exception), error)

    def test_inspect_refuses_growth(self):
        """C1: Refuse a capture growing between the descriptor size check and read."""
        fstat = os.fstat

        def grow(fd):
            info = fstat(fd)
            if info.st_ino == live_inode:
                with self.live.open("ab") as stream:
                    stream.truncate(1024 * 1024 + 1)
            return info

        live_inode = self.live.stat().st_ino
        with patch("campaign_tool.records.golive_pair.os.fstat", side_effect=grow):
            with self.assertRaises(ValueError) as caught:
                golive_pair.inspect(self.wrapper, self.live)
        self.assertIs(type(caught.exception), ValueError)

    def test_inspect_refuses_link_at_open(self):
        """C1: Do not follow a link if the leaf changes after lstat."""
        real_open = os.open
        link = self.live.parent / "synthetic-link"
        link.symlink_to(self.wrapper)

        def redirect(path, flags):
            return real_open(link if Path(path) == self.live else path, flags)

        with patch("campaign_tool.records.golive_pair.os.open", side_effect=redirect):
            with self.assertRaises(OSError) as caught:
                golive_pair.inspect(self.wrapper, self.live)
        self.assertIs(type(caught.exception), OSError)
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def _compare(self, **delta):
        result = cast(golive_pair.PairResult, {**_result(_WRAPPER, _LIVE, [_SHORT], True), **delta})
        return golive_pair.compare(result, _EXPECTED)

    def test_compare_match(self):
        """C2: Matching hashes and engine produce only the match code."""
        self.assertEqual(self._compare(), (True, ["PAIR_MATCH"]))

    def test_compare_wrapper_drift(self):
        """C2: Detect wrapper-only drift without mislabelling it LIVE drift."""
        self.assertEqual(self._compare(wrapper_sha256="0" * 64), (False, ["PAIR_WRAPPER_DRIFT"]))

    def test_compare_live_drift(self):
        """C2: Detect LIVE-only drift even when the wrapper hash is unchanged."""
        self.assertEqual(self._compare(live_sha256="1" * 64), (False, ["PAIR_LIVE_DRIFT"]))

    def test_compare_mixed_engine(self):
        """C2: An approved ID among mixed IDs, or no ID at all, cannot match."""
        for engines in ([_SHORT, _FULL], []):
            with self.subTest(engines=engines):
                self.assertEqual(self._compare(engines=engines, consistent=False), (False, ["PAIR_ENGINE_MIXED"]))

    def test_compare_engine_drift(self):
        """C2: Matching hashes do not excuse a wrong engine pin."""
        self.assertEqual(self._compare(engines=[_FULL]), (False, ["PAIR_ENGINE_DRIFT"]))

    def test_compare_combined_drift(self):
        """C2: Do not stop after the first independent drift."""
        drift = self._compare(wrapper_sha256="0" * 64, live_sha256="1" * 64,
                              engines=[_SHORT, _FULL], consistent=False)
        self.assertEqual(drift, (False, ["PAIR_WRAPPER_DRIFT", "PAIR_LIVE_DRIFT", "PAIR_ENGINE_MIXED"]))

    def _cli(self, *options):
        return self._run("--wrapper", str(self.wrapper), "--live", str(self.live), *options)

    def _run(self, *arguments):
        run = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records.golive_pair", *arguments],
                             capture_output=True, text=True, timeout=5)
        return run.returncode, run.stdout, run.stderr

    def _expected_report(self, *codes):
        return {
            "wrapper_sha256": hashlib.sha256(self.wrapper.read_bytes()).hexdigest(),
            "live_sha256": hashlib.sha256(self.live.read_bytes()).hexdigest(),
            "engines": [_SHORT],
            "codes": list(codes),
        }

    def test_cli_with_pin(self):
        """C3: Return zero for the approved pin and one for a stale pin, even with one engine."""
        for wrapper_hash, status, code in (
            (_EXPECTED["wrapper_sha256"], 0, "PAIR_MATCH"),
            ("0" * 64, 1, "PAIR_WRAPPER_DRIFT"),
        ):
            with self.subTest(code=code):
                status_out, stdout, stderr = self._cli(
                    "--json", "--expect-wrapper", wrapper_hash, "--expect-live", _EXPECTED["live_sha256"],
                    "--expect-engine", _SHORT)
                self.assertEqual((status_out, json.loads(stdout), stderr),
                                 (status, self._expected_report(code), ""))

    def test_cli_without_pin(self):
        """C3: Unpinned checks still refuse mixed captures and accept one engine."""
        status, stdout, stderr = self._cli("--json")
        self.assertEqual((status, json.loads(stdout), stderr), (0, self._expected_report("PAIR_MATCH"), ""))
        self.live.write_bytes(f'{{"path":"/fixture/{_FULL}"}}'.encode())
        status, stdout, stderr = self._cli("--json")
        report = {**self._expected_report("PAIR_ENGINE_MIXED"), "engines": [_SHORT, _FULL]}
        self.assertEqual((status, json.loads(stdout), stderr), (1, report, ""))

    def test_cli_bad_input(self):
        """C3: Distinguish CLI and file refusals from drift; partial pins cannot pass."""
        args = ["--expect-wrapper", _EXPECTED["wrapper_sha256"], "--expect-live", _EXPECTED["live_sha256"],
                "--expect-engine", _SHORT]
        refused = (2, "", "PAIR_BAD_INPUT\n")
        self.assertEqual(self._run("--wrapper", str(self.wrapper)), refused)
        for index in (1, 3, 5):
            with self.subTest(field=args[index - 1]):
                invalid = args.copy()
                invalid[index] = "synthetic-invalid-pin"
                self.assertEqual(self._cli(*invalid), refused)
                self.assertEqual(self._cli(*(args[:index - 1] + args[index + 1:])), refused)
        self.assertEqual(self._cli("--live", str(self.live.parent / "missing")), refused)

    def test_cli_output_boundary(self):
        """C4: Only allowlisted metadata on success or a fixed code on failure."""
        report = self._expected_report("PAIR_MATCH")
        want = (f"wrapper_sha256: {report['wrapper_sha256']}\n"
                f"live_sha256: {report['live_sha256']}\n"
                f"engines: {_SHORT}\n"
                "codes: PAIR_MATCH\n")
        self.assertEqual(self._cli(), (0, want, ""))
        self.assertEqual(self._cli("--json", "synthetic-private-argument"), (2, "", "PAIR_BAD_INPUT\n"))
