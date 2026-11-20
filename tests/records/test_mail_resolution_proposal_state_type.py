"""CLI contracts for proposal states in a synthetic canonical census."""
import hashlib
import json

from tests.records import test_mail_resolution_proposal as fixtures


class ProposalStateTypeTests(fixtures.Workdir):
    def run_proposal(self, state, output):
        entry = fixtures.entry("proposed", 1)
        entry["proposal"]["state"] = state
        inventory = self.inv(fixtures.census({fixtures.h("p"): entry}))
        return self.run_main("--write-resolution-policy-proposal", output,
                             "--from-inventory", inventory, "--approval-record", self.approval)

    def assert_state_refused(self, state):
        # Unhashable JSON states must take the coded refusal path, not escape
        # as TypeError from the state membership check.
        output = self.out / "proposal.json"
        code, stdout, stderr = self.run_proposal(state, output)
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(json.loads(stdout), {
            "source": "mail-history",
            "status": "refused",
            "code": "resolution_proposal_input_invalid",
        })
        self.assertFalse(output.exists())

    def test_array_state_is_refused(self):
        self.assert_state_refused([])

    def test_object_state_is_refused(self):
        self.assert_state_refused({})

    def test_proposed_state_preserves_output_bytes(self):
        output = self.out / "proposal.json"
        code, stdout, stderr = self.run_proposal("proposed", output)
        self.assertEqual((code, stderr), (0, ""))
        # Byte hash of the same fixture on pre-fix main (48bd7f46).
        expected_sha = "d0f7d07d1c5802ad7ff42f6ecb14c7359fad2cfb2bb6455ac2a5b1977a4a3c03"
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), expected_sha)
        self.assertEqual(json.loads(stdout)["proposal_sha256"], expected_sha)
