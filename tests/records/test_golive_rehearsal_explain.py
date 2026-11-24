"""Plain-words explanation of rehearsal receipts: C1 lines, C2 text per code, C3 offline next steps, C4 CLI."""

import hashlib
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import golive_rehearsal_explain as explain_mod
from campaign_tool.records import rehearsal_receipt, rehearse
from campaign_tool.records.run_stages import SANDBOX_MARKER

ENGINE = "a" * 40
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
SECRET_STEP = "step_home_secret_name"
SECRET_PATH = "secret_dir/secret_output.bin"
# Words that would tell someone to act on a live system. `next` text must avoid all of them.
LIVE_WORDS = re.compile(r"\b(start|install|switch|approve|enable|deploy|publish|send|activate|live)\b", re.I)


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = explain_mod.main([str(arg) for arg in args])
    return status, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root.chmod(0o700)

    def sandbox(self, name=None, marker=True):
        self.count = getattr(self, "count", 0) + 1
        path = self.root / (name or f"sb{self.count}")
        path.mkdir(mode=0o700)
        if marker:
            (path / SANDBOX_MARKER).touch(mode=0o600)
        return path

    def build(self, *, steps=None, manual=(), codes=(), marker=True, engine=ENGINE, outputs=(), name=None):
        sandbox = self.sandbox(name, marker)
        if steps is None:
            steps = [{"name": SECRET_STEP, "code": "OK", "outputs": list(outputs)}]
        result = {"stages": ["A6"], "steps": steps, "manual_steps": list(manual), "codes": list(codes)}
        return rehearsal_receipt.build("rehearse_x", result, sandbox, NOW, NOW, engine)


class C1Lines(Base):
    def test_c1_qualified_receipt_is_one_line(self):
        """C1: a clean receipt says only QUALIFIED. Bug caught: reasons printed for a good run."""
        self.assertEqual(explain_mod.explain(self.build()), ["QUALIFIED"])

    def test_c1_one_reason_line_per_code_in_receipt_order(self):
        """C1: reasons follow qualify_codes order. Bug caught: reordered or merged reasons."""
        receipt = self.build(steps=[{"name": SECRET_STEP, "code": "BAD", "outputs": []}], marker=False, engine=None)
        self.assertEqual(receipt["qualify_codes"],
                         ["REHEARSAL_FAILED_STEP", "REHEARSAL_NOT_SANDBOX", "REHEARSAL_ENGINE_UNKNOWN"])
        lines = explain_mod.explain(receipt)
        self.assertEqual(lines[0], "NOT QUALIFIED")
        self.assertEqual(len(lines), 4)
        for line, code in zip(lines[1:], receipt["qualify_codes"]):
            reason, action = explain_mod._TEXT[code]
            self.assertEqual(line, f"reason: {reason}; next: {action}")

    def test_c1_expected_refusals_are_observations_not_failures(self):
        """C1: codes from a passing negative step are shown as observed refusals. Bug caught: shown as reasons."""
        receipt = self.build(codes=["PHONE_REPLAY", "SIGNATURE_REFUSED"])
        self.assertEqual(explain_mod.explain(receipt), [
            "QUALIFIED",
            "observed (expected refusal): PHONE_REPLAY",
            "observed (expected refusal): SIGNATURE_REFUSED",
        ])
        # Still not qualified for another reason: refusals stay observations, not reasons.
        lines = explain_mod.explain(self.build(codes=["PHONE_REPLAY"], engine=None))
        self.assertEqual(lines[0], "NOT QUALIFIED")
        self.assertEqual([line.split(":")[0] for line in lines[1:]], ["reason", "observed (expected refusal)"])
        self.assertEqual(lines[-1], "observed (expected refusal): PHONE_REPLAY")

    def test_c1_codes_of_a_failed_run_are_not_called_expected(self):
        """C1: with result fail nothing is an expected refusal. Bug caught: failure codes called expected."""
        receipt = self.build(steps=[{"name": SECRET_STEP, "code": "BAD", "outputs": []}], codes=["SOME_CODE"])
        self.assertEqual(receipt["result"], "fail")
        self.assertFalse([line for line in explain_mod.explain(receipt) if line.startswith("observed")])


class C2TextPerCode(Base):
    def test_c2_every_known_code_has_text_and_next_action(self):
        """C2: the code tuple covers every receipt code and the driver error. Bug caught: new code with no text."""
        self.assertEqual(set(explain_mod.CODES), {*rehearsal_receipt._CODES, rehearse._DRIVER_ERROR})
        self.assertEqual(len(explain_mod.CODES), len(set(explain_mod.CODES)))
        sentences = set()
        for code in explain_mod.CODES:
            with self.subTest(code=code):
                receipt = dict(self.build(), qualified=False, qualify_codes=[code])
                line = explain_mod.explain(receipt)[1]
                reason, sep, action = line.removeprefix("reason: ").partition("; next: ")
                self.assertTrue(line.startswith("reason: ") and sep)
                self.assertGreater(len(reason.split()), 4)
                self.assertGreater(len(action.split()), 3)
                self.assertNotIn(code, line)
                sentences.add(reason)
        self.assertEqual(len(sentences), len(explain_mod.CODES))

    def test_c2_unknown_code_is_named_without_raising(self):
        """C2: an unknown code prints a fixed line. Bug caught: KeyError on a future code."""
        receipt = dict(self.build(), qualified=False, qualify_codes=["REHEARSAL_FUTURE_CODE"])
        self.assertEqual(explain_mod.explain(receipt), ["NOT QUALIFIED", "unrecognized code REHEARSAL_FUTURE_CODE"])

    def test_c2_no_paths_or_step_outputs_in_text(self):
        """C2: step names, output paths and odd codes never reach the text. Bug caught: leaking receipt contents."""
        sandbox = self.sandbox("leak")
        (sandbox / "secret_dir").mkdir()
        (sandbox / SECRET_PATH).write_bytes(b"x")
        digest = hashlib.sha256(b"y").hexdigest()  # wrong on purpose: output mismatch
        steps = [{"name": SECRET_STEP, "code": "BAD", "outputs": [{"path": SECRET_PATH, "sha256": digest}]}]
        result = {"stages": ["A6"], "steps": steps, "manual_steps": ["do the secret thing"], "codes": []}
        receipt = rehearsal_receipt.build("rehearse_x", result, sandbox, NOW, NOW, None)
        receipt["qualify_codes"].append("/home/user/private/path")
        text = "\n".join(explain_mod.explain(receipt) + explain_mod.explain(dict(receipt, result="pass", codes=["/etc/x"])))
        self.assertGreaterEqual(len(receipt["qualify_codes"]), 5)
        for leak in (SECRET_STEP, SECRET_PATH, "secret", "/home", "/etc", str(self.root), "rehearse_x"):
            self.assertNotIn(leak, text)
        self.assertNotIn("/", text.replace("`records rehearse --stage X`", ""))


class C3OfflineNext(unittest.TestCase):
    def test_c3_next_actions_are_offline(self):
        """C3: no next step names a live action; each is a re-run or an inspection. Bug caught: 'start/install/approve' advice."""
        for code, (_reason, action) in explain_mod._TEXT.items():
            with self.subTest(code=code):
                self.assertIsNone(LIVE_WORDS.search(action))
                self.assertRegex(action, r"re-run|inspect|read the receipt")
        self.assertIn("14 days", explain_mod._TEXT["REHEARSAL_STALE"][1])
        for code in ("REHEARSAL_ENGINE_UNKNOWN", "REHEARSAL_ENGINE_MISMATCH", "REHEARSAL_STALE"):
            self.assertIn("`records rehearse --stage X` from a git checkout", explain_mod._TEXT[code][1])
        self.assertIn("failed step's name", explain_mod._TEXT["REHEARSAL_FAILED_STEP"][1])


class C4Cli(Base):
    def write(self, receipt):
        out = self.root / f"receipts{len(list(self.root.iterdir()))}"
        out.mkdir()
        return rehearsal_receipt.write(receipt, out)

    def test_c4_exit_codes_and_output(self):
        """C4: 0 qualified, 1 not qualified. Bug caught: wrong exit status or lines not printed."""
        status, out, err = run_cli(self.write(self.build()))
        self.assertEqual((status, out, err), (0, "QUALIFIED\n", ""))
        path = self.write(self.build(engine=None))
        status, out, err = run_cli(path)
        self.assertEqual((status, err), (1, ""))
        self.assertEqual(out.splitlines(), explain_mod.explain(rehearsal_receipt.read(path)))
        self.assertEqual(out.splitlines()[0], "NOT QUALIFIED")

    def test_c4_unreadable_or_invalid_receipt_exits_2_without_leaks(self):
        """C4: unreadable or invalid input exits 2 and echoes no path. Bug caught: traceback or leaked path."""
        good = self.write(self.build())
        bad_schema = self.root / "bad.json"
        bad_schema.write_text(json.dumps(dict(rehearsal_receipt.read(good), qualified=False)), encoding="utf-8")
        not_json = self.root / "garbage.json"
        not_json.write_text("{nope", encoding="utf-8")
        for target in (self.root / "missing.json", self.root, not_json, bad_schema):
            with self.subTest(target=target.name):
                status, out, err = run_cli(target)
                self.assertEqual((status, out), (2, ""))
                self.assertEqual(err, "golive_rehearsal_explain: unreadable or invalid rehearsal receipt\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            run_cli()
        self.assertEqual(raised.exception.code, 2)

    def test_c4_is_read_only(self):
        """C4: the receipt and its folder are untouched. Bug caught: the explainer rewriting evidence."""
        path = self.write(self.build())
        before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in self.root.rglob("*")))
        run_cli(path)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in self.root.rglob("*"))))


class RealDriverRuns(Base):
    """Two real phone-signing runs: it records two expected refusals in a passing step."""

    def run_driver(self, engine):
        with patch.object(rehearsal_receipt, "engine_identity", return_value=engine), \
                redirect_stdout(io.StringIO()) as out:
            status = rehearse.main(["--out", str(self.root / "runs"), "--stage", "A6", "--json"])
        row = json.loads(out.getvalue().splitlines()[0])
        return status, row["receipt"]

    def test_c1_c4_real_driver_qualified_with_synthetic_engine(self):
        """C1/C4: a qualified real run lists its refusals as observations. Bug caught: refusals shown as failures."""
        status, path = self.run_driver(ENGINE)
        self.assertEqual(status, 0)
        code, out, _ = run_cli(path)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "QUALIFIED",
            "observed (expected refusal): PHONE_REPLAY",
            "observed (expected refusal): SIGNATURE_REFUSED",
        ])

    def test_c1_c4_real_driver_unknown_engine_is_not_qualified(self):
        """C1/C4: a real run outside a clean checkout says why and what to do. Bug caught: bare code only."""
        status, path = self.run_driver(None)
        self.assertEqual(status, 1)
        code, out, _ = run_cli(path)
        lines = out.splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "NOT QUALIFIED")
        reason, action = explain_mod._TEXT["REHEARSAL_ENGINE_UNKNOWN"]
        self.assertEqual(lines[1], f"reason: {reason}; next: {action}")
        self.assertEqual(lines[2:], ["observed (expected refusal): PHONE_REPLAY",
                                     "observed (expected refusal): SIGNATURE_REFUSED"])
        self.assertNotIn("REHEARSAL_ENGINE_UNKNOWN", out)


if __name__ == "__main__":
    unittest.main()
