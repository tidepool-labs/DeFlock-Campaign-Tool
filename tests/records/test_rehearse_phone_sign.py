"""Runtime contracts for the A6 synthetic phone-signing rehearsal."""
import hashlib
import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from campaign_tool import approval, phone_approval
from campaign_tool.records import approval_queue, console_apply, console_journal, console_phone, phone_enrol, run_stages
from campaign_tool.records import rehearse_phone_sign as driver
from campaign_tool.records import rehearsal_trust


class PhoneRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir(mode=0o700)

    def output(self, path):
        return {"path": path.relative_to(self.sandbox).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def expected_steps(self):
        record, = self.sandbox.glob("phone-enrol-*.json")
        return [
            {"name": "enrol", "code": "OK", "outputs": [self.output(record), self.output(self.sandbox / "rehearsal.allowed_signers")]},
            {"name": "phone_approve", "code": "OK", "outputs": []},
            {"name": "replay_refused", "code": "OK", "outputs": []},
            {"name": "unenrolled_refused", "code": "OK", "outputs": [self.output(self.sandbox / "journal" / console_journal.FILE)]},
        ]

    # Catches a missing/misordered step, fake success without applying the journal, or a receipt missing the actual public outputs.
    def test_journey(self):
        """C1: the CLI completes all four steps and only the enrolled draft advances."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(driver.main(["--sandbox", str(self.sandbox)]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"stages": ["A6"], "steps": self.expected_steps(),
                                                     "manual_steps": [], "codes": ["PHONE_REPLAY", "SIGNATURE_REFUSED"]})
        project = self.sandbox / "project"
        entries = console_journal.read(self.sandbox / "journal")
        self.assertEqual([e["action"] for e in entries], ["phone_approve", "phone_approve"])
        workspace = SimpleNamespace(TemporaryDirectory=partial(tempfile.TemporaryDirectory, dir=self.sandbox))
        with rehearsal_trust.sandbox_anchor(self.sandbox, self.sandbox / "rehearsal.allowed_signers"), \
                patch.object(approval, "tempfile", workspace):
            rows = {row["id"]: (row["status"], row.get("approved_by")) for row in approval_queue.load_queue(project)}
        self.assertEqual(rows, {entries[0]["target_id"]: ("approved", "rehearsal-phone"),
                                entries[1]["target_id"]: ("pending", None)})

    # Catches a generic 409 being counted as the replay guard, or re-signing instead of posting the identical tap twice.
    def test_replay_mapping(self):
        """C2: the unchanged POST gives exactly HTTP 409 phone_replay for the identical assertion."""
        seen = []
        real_post = console_phone.post

        def capture(handler):
            seen.append((dict(handler._body()), None))
            fail = handler._fail

            def record(status, code=None, **kwargs):
                seen[-1] = (seen[-1][0], (status, code))
                return fail(status, code, **kwargs)

            handler._fail = record
            return real_post(handler)

        with patch.object(console_phone, "post", side_effect=capture):
            result = driver.run(self.sandbox)
        self.assertEqual([refusal for _, refusal in seen], [None, (409, "phone_replay"), None])
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertEqual(result["steps"][2], {"name": "replay_refused", "code": "OK", "outputs": []})
        self.assertEqual(result["codes"], ["PHONE_REPLAY", "SIGNATURE_REFUSED"])

    # Catches reporting a refusal without reaching apply, using an expired/reused envelope, or signing with the enrolled key again.
    def test_unenrolled_mapping(self):
        """C2: real apply reports exactly SIGNATURE_REFUSED for a fresh tap from a different key."""
        observed = []
        real_apply = console_apply.outcome

        def capture(project, entry):
            value = real_apply(project, entry)
            observed.append(value)
            return value

        with patch.object(console_apply, "outcome", side_effect=capture), patch.object(driver, "_tap", wraps=driver._tap) as tap:
            result = driver.run(self.sandbox)
        self.assertEqual(observed, ["APPLIED", "SIGNATURE_REFUSED"])
        self.assertEqual(tap.call_count, 2)
        self.assertNotEqual(tap.call_args_list[0].args[0], tap.call_args_list[1].args[0])
        entries = console_journal.read(self.sandbox / "journal")
        envelopes = [phone_approval.tap_envelope(json.loads(e["value"])) for e in entries]
        self.assertNotEqual(envelopes[0]["nonce"], envelopes[1]["nonce"])
        self.assertEqual([env["decision"] for env in envelopes], [entry["target_id"] for entry in entries])
        self.assertEqual(result["steps"][-1], self.expected_steps()[-1])
        self.assertEqual(result["codes"], ["PHONE_REPLAY", "SIGNATURE_REFUSED"])

    # Catches inventorying the private signer, dropping the generated public record, or widening the phone trust namespace.
    def test_public_artifact_allowlist(self):
        """C3: only the public enrolment, public trust line and synthetic journal are listed and hashed."""
        original_signers = approval.TRUSTED_SIGNERS
        with patch.object(driver, "_write_private", wraps=driver._write_private) as writer, \
                patch("socket.socket", side_effect=AssertionError("unexpected socket")):
            result = driver.run(self.sandbox)
        marker = self.sandbox / run_stages.SANDBOX_MARKER
        self.assertEqual(writer.call_args_list[0].args[0], marker)
        self.assertEqual(run_stages.sandbox_refusal(self.sandbox), None)
        self.assertEqual(list(self.root.iterdir()), [self.sandbox])
        self.assertEqual(result["steps"], self.expected_steps())
        record, = self.sandbox.glob("phone-enrol-*.json")
        self.assertEqual(tuple(sorted(json.loads(record.read_bytes()))), phone_enrol.RECORD_FIELDS)
        signers = (self.sandbox / "rehearsal.allowed_signers").read_text().splitlines()
        self.assertEqual(len(signers), 2)
        self.assertEqual(signers[1], phone_enrol.line(phone_enrol.load(record), "rehearsal-phone"))
        private = self.sandbox / "typed-rehearsal"
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertEqual(approval.TRUSTED_SIGNERS, original_signers)

    # A refused or missing prerequisite must leave later steps unattempted, not just add a diagnostic to an otherwise green run.
    def test_stops_at_first_failed_step(self):
        """C1: each failed step returns an exact failure receipt and stops before subsequent steps."""
        names = ["enrol", "phone_approve", "replay_refused", "unenrolled_refused"]
        for index, name in enumerate(names):
            with self.subTest(step=name):
                self.sandbox = self.root / name
                self.sandbox.mkdir(mode=0o700)
                if index == 0:
                    fault = patch.object(phone_enrol, "store", side_effect=OSError("not emitted"))
                elif index == 2:
                    # Not all 409 responses are replay evidence.
                    post = driver._post
                    def wrong_conflict(*args):
                        result = post(*args)
                        return ((409, "phone_mismatch"),) if result == ((409, "phone_replay"),) else result
                    fault = patch.object(driver, "_post", side_effect=wrong_conflict)
                else:
                    outcomes = [None] if index == 1 else ["APPLIED", "INVALID_TRANSITION"]
                    real_apply = console_apply.outcome
                    def wrong_outcome(project, entry):
                        outcome = outcomes.pop(0)
                        return real_apply(project, entry) if outcome == "APPLIED" else outcome
                    fault = patch.object(console_apply, "outcome", side_effect=wrong_outcome)
                with fault:
                    result = driver.run(self.sandbox)
                code = name.upper() + "_FAILED"
                earlier = [{"name": prior, "code": "OK", "outputs": []} for prior in names[:index]]
                if earlier:
                    record, = self.sandbox.glob("phone-enrol-*.json")
                    earlier[0]["outputs"] = [self.output(record), self.output(self.sandbox / "rehearsal.allowed_signers")]
                observed = ["PHONE_REPLAY"] if index == 3 else []
                self.assertEqual(result, {"stages": ["A6"], "manual_steps": [], "codes": observed + [code],
                                          "steps": earlier + [{"name": name, "code": code, "outputs": []}]})

    # Catches the CLI treating a red or manually incomplete run as qualified, or hiding the failed receipt.
    def test_cli_unqualified(self):
        """C1: the CLI emits the failure or manual-step result and exits 1."""
        for steps, manual, codes in (([{"name": "enrol", "code": "ENROL_FAILED", "outputs": []}], [], ["ENROL_FAILED"]),
                                     ([], ["manual check"], [])):
            with self.subTest(manual=manual):
                expected = {"stages": ["A6"], "steps": steps, "manual_steps": manual, "codes": codes}
                out = io.StringIO()
                with patch.object(driver, "run", return_value=expected), redirect_stdout(out):
                    self.assertEqual(driver.main(["--sandbox", str(self.root / "new")]), 1)
                self.assertEqual(json.loads(out.getvalue()), expected)
                self.assertEqual(stat.S_IMODE((self.root / "new").stat().st_mode), 0o700)

    # Catches marker or key creation in a nonempty directory, possibly alongside existing approval files.
    def test_existing_sandbox_refused(self):
        """C3: a nonempty sandbox is refused without changing it, at both entrypoints."""
        sentinel = self.sandbox / "sentinel"
        sentinel.write_bytes(b"synthetic sentinel")
        with self.assertRaises(ValueError) as error:
            driver.run(self.sandbox)
        self.assertIs(type(error.exception), ValueError)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            driver.main(["--sandbox", str(self.sandbox)])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(list(self.sandbox.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"synthetic sentinel")
