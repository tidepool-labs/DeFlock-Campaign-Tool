"""Exercise document receipt publication on disk with a synthetic PDF."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.container_host import Rejected
from campaign_tool.records.intake import document_drop as drop
from tests.records.private_fixture import private_fixture_parent


class AtomicDocumentReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=private_fixture_parent())
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        (self.output / "blobs").mkdir(mode=0o700)
        self.receipts = self.output / "document-receipts"
        self.source = self.output / "synthetic-source"
        self.raw = b"%PDF-1.4\n% synthetic receipt fixture\n%%EOF\n"
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.entry = {"name": "example.pdf", "sha256": self.sha, "bytes": len(self.raw)}
        manifest_sha = hashlib.sha256(b"synthetic manifest").hexdigest()
        source_stat = (1, 2, len(self.raw), 3, 4)
        self.manifest = {"entries": [self.entry], "sha256": manifest_sha,
                         "signature": (None, ((self.entry["name"], source_stat),))}
        self.receipt = {"schema": "document-drop-receipt-v1", "form": "pdf",
                        "sha256": self.sha, "bytes": len(self.raw), "source_stat": source_stat,
                        "source": {"schema": "document-drop-source-v1", "root": str(self.source),
                                   "name": "example.pdf", "manifest_sha256": manifest_sha}}
        self.body = json.dumps(self.receipt, sort_keys=True, separators=(",", ":")).encode()
        self.receipt_sha = hashlib.sha256(self.body).hexdigest()
        self.final = self.receipts / self.receipt_sha
        reader = patch.object(drop, "read_entry", return_value=self.raw)
        reader.start()
        self.addCleanup(reader.stop)

    def capture(self):
        return drop.capture(self.output, self.source, self.manifest, self.entry)

    def assert_complete_receipt(self, result):
        self.assertEqual(result, (self.receipt, self.receipt_sha, str(self.final)))
        self.assertEqual(list(self.receipts.iterdir()), [self.final])
        self.assertEqual(self.final.read_bytes(), self.body)
        self.assertEqual(stat.S_IMODE(self.final.stat().st_mode), 0o600)
        self.assertEqual(self.final.stat().st_nlink, 1)

    def test_partial_write_leaves_sealed_blob_and_retryable_receipt(self):
        # Regresses if a partial write can reach the final name before fsync.
        fdopen = os.fdopen

        @contextmanager
        def interrupt_receipt(fd, mode):
            with fdopen(fd, mode) as stream:
                write = stream.write

                def write_prefix(raw):
                    if raw == self.body:
                        write(raw[:17])
                        stream.flush()
                        raise OSError(errno.ENOSPC, "synthetic partial receipt write")
                    return write(raw)

                with patch.object(stream, "write", side_effect=write_prefix):
                    yield stream

        with patch.object(drop.os, "fdopen", side_effect=interrupt_receipt):
            with self.assertRaisesRegex(OSError, "synthetic partial receipt write"):
                self.capture()
        self.assertEqual((self.output / "blobs" / self.sha).read_bytes(), self.raw)
        self.assertEqual(list(self.receipts.iterdir()), [])
        self.assert_complete_receipt(self.capture())

    def test_existing_conflict_is_not_replaced(self):
        # A different body at the expected hash-name is refused, not repaired in place.
        self.receipts.mkdir(mode=0o700)
        self.final.write_bytes(b"{}")
        self.final.chmod(0o600)
        inode = self.final.stat().st_ino
        with self.assertRaises(Rejected):
            self.capture()
        self.assertEqual(self.final.read_bytes(), b"{}")
        self.assertEqual(self.final.stat().st_ino, inode)
        self.assertEqual(list(self.receipts.iterdir()), [self.final])

    def test_publication_and_directory_fsync_order(self):
        # Observe complete private bytes before publication and the final name at dir sync.
        fsync = os.fsync
        events = []

        def observe_fsync(fd):
            info = os.fstat(fd)
            if self.receipts.exists():
                if stat.S_ISREG(info.st_mode):
                    for staged in self.receipts.iterdir():
                        if staged.stat().st_ino == info.st_ino:
                            self.assertFalse(self.final.exists())
                            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                            self.assertEqual(staged.read_bytes(), self.body)
                            events.append("staged file")
                elif info.st_ino == self.receipts.stat().st_ino:
                    self.assertEqual(self.final.read_bytes(), self.body)
                    events.append("receipt directory")
            fsync(fd)

        with patch.object(drop.os, "fsync", side_effect=observe_fsync):
            self.assert_complete_receipt(self.capture())
        self.assertEqual(events, ["staged file", "receipt directory"])

        # An identical retry also syncs the directory, without another receipt write.
        with patch.object(drop.os, "fsync", side_effect=observe_fsync):
            self.assert_complete_receipt(self.capture())
        self.assertEqual(events, ["staged file", "receipt directory", "receipt directory"])

    def test_concurrent_receipt_is_compared_without_replacement(self):
        # A competitor can win after the initial miss; both matching and conflicting wins stay intact.
        fsync = os.fsync
        for winner in (self.body, b"{}"):
            with self.subTest(matches=winner == self.body):
                observed = {}

                def publish_winner(fd):
                    fsync(fd)
                    info = os.fstat(fd)
                    if not self.receipts.exists():
                        return
                    if stat.S_ISREG(info.st_mode):
                        with self.final.open("xb") as stream:
                            os.fchmod(stream.fileno(), 0o600)
                            stream.write(winner)
                            stream.flush()
                            fsync(stream.fileno())
                        observed["inode"] = self.final.stat().st_ino
                    elif info.st_ino == self.receipts.stat().st_ino:
                        observed["directory_synced"] = True

                with patch.object(drop.os, "fsync", side_effect=publish_winner):
                    if winner == self.body:
                        self.assert_complete_receipt(self.capture())
                        self.assertTrue(observed.get("directory_synced"))
                    else:
                        with self.assertRaises(Rejected):
                            self.capture()
                self.assertEqual(list(self.receipts.iterdir()), [self.final])
                self.assertEqual(self.final.stat().st_ino, observed["inode"])
                self.assertEqual(self.final.read_bytes(), winner)
                self.final.unlink()
                self.receipts.rmdir()
