"""Top-level byte counts on producer-written v2 receipts."""
import json
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.intake import mail_delta, native_receipt


class NativeReceiptByteCountTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        raw = b"Subject: Synthetic byte count\r\n\r\n".ljust(121, b"x") + b"\r\n"
        self.path = native_receipt.export(
            raw, mail_root=root / "mail", account="synthetic", mailbox="INBOX",
            uidvalidity=1, uid=2, original_format="eml",
        )
        self.receipt = json.loads(self.path.read_bytes())
        self.assertEqual(self.receipt["schema"], "mail-wire-receipt-v2")
        self.assertEqual(self.receipt["original_eml"]["bytes"], 123)

    def test_producer_written_receipt_loads_unchanged(self):
        original_bytes = self.path.read_bytes()
        receipt, identity, items = mail_delta.load_receipt(self.path)
        self.assertEqual(receipt, self.receipt)
        self.assertIs(type(receipt["bytes"]), int)
        self.assertEqual(receipt["bytes"], 123)
        self.assertEqual(identity, ("synthetic", "INBOX", "1", "2"))
        self.assertEqual([item["size"] for item in items], [123])
        self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_rejects_invalid_top_level_byte_counts(self):
        # Numeric equality alone accepts 123.0; the field must be an exact int
        # (not bool) and still agree with the unchanged nested byte count.
        for byte_count in (123.0, True, 124):
            with self.subTest(byte_count=byte_count):
                self.path.write_text(json.dumps({**self.receipt, "bytes": byte_count}),
                                     encoding="utf-8")
                with self.assertRaisesRegex(mail_delta.Rejected, "^receipt_bytes_mismatch$"):
                    mail_delta.load_receipt(self.path)
