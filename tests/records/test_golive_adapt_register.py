import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from campaign_tool.records import golive_adapt_register as adapter
from campaign_tool.records.outcomes import register as producer


# Jan 12 in UTC, but still Jan 11 on the register's calendar.
NOW = datetime(2030, 1, 12, 1, tzinfo=timezone.utc)
SCHEMA = "records-request-register-v1"
BASENAME = "CURRENT-REQUEST-REGISTER.json"


def register(*rows):
    return {"schema": SCHEMA, "requests": list(rows)}


def expected(doc, result, codes, *, raw=None, name=BASENAME):
    if raw is None:
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema": "records-golive-evidence-v1", "stage": "1", "kind": "live",
        "result": result, "recorded_at": "2030-01-12T01:00:00Z", "engine": None,
        "actor": "pipeline", "codes": codes,
        "source": {"schema": SCHEMA, "name": name, "sha256": hashlib.sha256(raw).hexdigest()},
    }


class RegisterEvidenceTest(unittest.TestCase):
    def test_c1_explicit_submission_from_producer(self):
        """C1: a producer row qualifies on its send date, not its clock date."""
        data = {table: [] for table in producer.correspondence.TABLES}
        data["agencies"] = [{"id": "a1", "name": "Synthetic Agency"}]
        data["requests"] = [{"id": "r1", "agency_id": "a1", "external_ref": "SYN-1",
                             "submitted_at": "2030-01-10", "received_at": "2030-01-12",
                             "status": "open"}]
        doc = producer.build_register(data, as_of="2030-01-12")
        self.assertEqual(adapter.records(doc, NOW), expected(doc, "pass", []))

    def test_c1_no_admitted_submission(self):
        """C1: an empty register, a receipt and a legacy proxy cannot prove send."""
        for doc in (register(), register({"submitted_at": None, "request_date": "2030-01-10"}),
                    register({"request_date": "2030-01-10", "deadline": {"basis": "submission_proxy"}})):
            with self.subTest(doc=doc):
                self.assertEqual(adapter.records(doc, NOW), expected(doc, "fail", ["REGISTER_NO_SENT"]))

    def test_c1_future_dates_do_not_count(self):
        """C1: today's Pacific send passes; a future row never supplies a pass."""
        tomorrow = {"submitted_at": "2030-01-12", "request_date": "2030-01-10"}
        today = {"submitted_at": "2030-01-11"}
        for doc, result in ((register(tomorrow), "fail"), (register(tomorrow, today), "pass"),
                            (register(today, tomorrow), "pass")):
            with self.subTest(doc=doc):
                self.assertEqual(adapter.records(doc, NOW), expected(doc, result, ["REGISTER_FUTURE_SENT"]))

    def test_c1_refuses_malformed_inputs(self):
        """C1: foreign receipts, invalid dates and ambiguous clocks raise ValueError."""
        cases = [(doc, NOW) for doc in (None, {"schema": "foreign", "requests": []},
                                       {"schema": SCHEMA}, register(None), register({"submitted_at": 1}),
                                       register({"submitted_at": "2030-02-31"}),
                                       register({"submitted_at": "20300111"}), register({"x": object()}))]
        cases.append((register(), NOW.replace(tzinfo=None)))
        for doc, now in cases:
            with self.subTest(doc=doc, now=now):
                with self.assertRaises(ValueError) as error:
                    adapter.records(doc, now)
                self.assertIs(type(error.exception), ValueError)

    def test_c2_metadata_only(self):
        """C2: never copy private row content into pipeline evidence."""
        doc = register({"submitted_at": "2030-01-11", "agency": "Synthetic Agency SECRET",
                        "request_id": "SYN-PRIVATE-IDENTIFIER", "asked": "Private synthetic scope"})
        self.assertEqual(adapter.records(doc, NOW), expected(doc, "pass", []))

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(adapter, "datetime", FrozenDateTime), redirect_stdout(stdout), redirect_stderr(stderr):
            status = adapter.main(list(args))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_c3_formats_and_exit_codes(self):
        """C3: TSV and JSON use sibling formats; JSON identifies exact saved bytes."""
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "synthetic-register.json"
            for doc, result, codes, status in ((register({"submitted_at": "2030-01-11"}), "pass", [], 0),
                                                (register(), "fail", ["REGISTER_NO_SENT"], 2)):
                with self.subTest(result=result):
                    raw = (json.dumps(doc, indent=2) + "\n").encode()
                    path.write_bytes(raw)
                    line = "\t".join(("1", "live", result, "2030-01-12T01:00:00Z", SCHEMA, ",".join(codes)))
                    self.assertEqual(self.cli(str(path)), (status, line + "\n", ""))
                    code, out, err = self.cli(str(path), "--json")
                    self.assertEqual((code, json.loads(out), err),
                                     (status, [expected(doc, result, codes, raw=raw, name=path.name)], ""))
                    self.assertEqual(path.read_bytes(), raw)

    def test_c3_refuses_unreadable_or_ambiguous_receipt(self):
        """C3: missing, invalid or duplicate-key input never emits evidence or raw diagnostics."""
        refusal = (2, "", "REGISTER_EVIDENCE_BAD_INPUT\n")
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / BASENAME
            self.assertEqual(self.cli(str(path)), refusal)
            for raw in (b"Synthetic Agency SECRET", b'{"schema":"records-request-register-v1",'
                        b'"requests":[],"requests":[{"submitted_at":"2030-01-11"}]}'):
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    self.assertEqual(self.cli(str(path), "--json"), refusal)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2030, 1, 12, 1, tzinfo=timezone.utc).astimezone(tz)
