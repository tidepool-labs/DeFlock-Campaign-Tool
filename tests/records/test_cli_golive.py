"""Offline tests of the records CLI dispatch boundary."""
import io
import sys
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import Mock, patch

from campaign_tool.records import cli


ROUTES = (
    ("golive", "golive_ledger"),
    ("golive-next", "golive_next"),
    ("golive-record", "golive_record"),
    ("golive-gate", "golive_gate"),
    ("rehearse", "rehearse"),
)


class GoLiveCliTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(cli.os, "umask"))

    def test_c1_dispatch(self):
        """C1: Every new verb selects its own main, not a sibling module."""
        for verb, module in ROUTES:
            with self.subTest(verb=verb):
                entry = Mock(return_value=0)
                with patch.object(sys, "argv", ["records", verb]), patch.object(
                    cli.importlib, "import_module", return_value=SimpleNamespace(main=entry)
                ) as load:
                    self.assertEqual(cli.main(), 0)
                load.assert_called_once_with(f".{module}", "campaign_tool.records")
                entry.assert_called_once_with()

    def test_c2_argument_and_exit_passthrough(self):
        """C2: Do not reparse arguments, reorder flags, or normalize exit codes."""
        tail = ["--label=a b", "--tag", "one", "--tag", "two", "--", "--help", ""]
        for verb, _ in ROUTES:
            for status in (0, 9):
                with self.subTest(verb=verb, status=status):
                    observed = []

                    def main():
                        observed.append(sys.argv[1:])
                        return status

                    with patch.object(sys, "argv", ["records", verb, *tail]), patch.object(
                        cli.importlib, "import_module", return_value=SimpleNamespace(main=main)
                    ):
                        self.assertEqual(cli.main(), status)
                    self.assertEqual(observed, [tail])

    def test_c3_legacy_dispatch(self):
        """C3: Keep both the default-main and named-entry-point legacy routes."""
        for verb, module, function in (
            ("version", "release_manifest", "main"),
            ("console-import", "console_export", "import_main"),
        ):
            with self.subTest(verb=verb):
                entry = Mock(return_value=4)
                with patch.object(sys, "argv", ["records", verb]), patch.object(
                    cli.importlib, "import_module", return_value=SimpleNamespace(**{function: entry})
                ) as load:
                    self.assertEqual(cli.main(), 4)
                load.assert_called_once_with(f".{module}", "campaign_tool.records")
                entry.assert_called_once_with()

    def test_c3_unknown_verb(self):
        """C3: An unknown verb still exits with a usage error before any import."""
        with patch.object(sys, "argv", ["records", "golive-unknown"]), patch.object(
            cli.importlib, "import_module"
        ) as load, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertEqual(raised.exception.code, 2)
        load.assert_not_called()
