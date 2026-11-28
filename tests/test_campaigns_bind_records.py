"""DF1125-05: `campaigns bind-records` / `unbind-records` bind a campaign to its records export by command.

Synthetic campaigns and a genuine synthetic console export in a temporary folder only; nothing is fetched, sent or
published. Run from the repository root: python3 -B -m unittest tests.test_campaigns_bind_records -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, registry  # noqa: E402
from campaign_tool.records import campaign_summary  # noqa: E402
from tests.records.test_campaign_summary import real_export  # noqa: E402


class BindRecords(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-bind-records-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.addCleanup(self.unlock, self.base)
        self.register = self.base / "config" / "campaigns.json"
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)
        for name in ("cedar", "pine"):
            root = self.base / name
            root.mkdir()
            (root / "campaign.json").write_text(json.dumps({"schema_version": 1, "name": name, "county": "Synthetic",
                                                            "state": "CA"}), encoding="utf-8")
            registry.add(name, root)
        self.made = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)

    @staticmethod
    def unlock(root):
        for base, dirs, names in os.walk(root):
            os.chmod(base, 0o700)

    def export(self, folder):
        sandbox = self.base / folder
        sandbox.mkdir()
        return real_export(sandbox, self.made)

    def run_cli(self, *arguments):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code or 0
            finally:
                registry.release()
        self.assertNotIn("Traceback", err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def entries(self):
        return json.loads(self.register.read_text(encoding="utf-8"))

    def refused(self, *arguments, rule):
        before = self.register.read_bytes()
        code, out, err = self.run_cli("campaigns", *arguments)
        self.assertEqual((code, out), (1, ""), err)
        self.assertIn(rule, err)
        self.assertEqual(self.register.read_bytes(), before)  # nothing written
        return err

    def test_bind_writes_only_that_entry_and_today_reads_it(self):
        root, pin = self.export("exports-cedar")
        pine_before = next(e for e in self.entries() if e["name"] == "pine")
        code, out, err = self.run_cli("campaigns", "bind-records", "cedar", "--export-root", root)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "campaign | cedar | bound to its records export | private; nothing was copied\n")
        cedar, pine = self.entries()
        self.assertEqual(cedar["records"], {"export_root": str(root), "source_project_id": pin.source_project_id})
        self.assertEqual(pine, pine_before)
        self.assertEqual(self.register.stat().st_mode & 0o777, 0o600)
        with mock.patch.object(registry, "_utcnow", return_value=self.made + timedelta(hours=1)):
            rows, current = registry.today_rows(self.made.date())
        records = [r for r in rows if r.get("kind") == "records_summary"]
        self.assertTrue(current)
        self.assertEqual([(r["campaign"], r["records"]["state"]) for r in records], [("cedar", "ok")])

    def test_rebind_needs_replace_and_unbind_removes_only_the_binding(self):
        first, _ = self.export("exports-a")
        second, pin = self.export("exports-b")  # the same synthetic project id: a rebind of the same campaign
        self.assertEqual(self.run_cli("campaigns", "bind-records", "cedar", "--export-root", first)[0], 0)
        self.refused("bind-records", "cedar", "--export-root", second, rule="already bound to a records export; use --replace")
        self.assertEqual(self.run_cli("campaigns", "bind-records", "cedar", "--export-root", second, "--replace")[0], 0)
        self.assertEqual(self.entries()[0]["records"]["export_root"], str(second))
        code, out, err = self.run_cli("campaigns", "unbind-records", "cedar")
        self.assertEqual((code, out), (0, "campaign | cedar | records binding removed; the export was not touched\n"), err)
        self.assertEqual([set(e) for e in self.entries()], [registry.KEYS, registry.KEYS])
        self.assertTrue((second / "current").is_symlink())  # the export is untouched
        self.refused("unbind-records", "cedar", rule="has no records binding")

    def test_refusals_name_the_rule_and_never_echo_paths_or_ids(self):
        root, pin = self.export("exports-cedar")
        self.assertEqual(self.run_cli("campaigns", "bind-records", "cedar", "--export-root", root)[0], 0)
        empty = self.base / "empty-root"
        empty.mkdir(mode=0o700)
        link = self.base / "linked-root"
        link.symlink_to(root)
        cases = [
            (("bind-records", "nosuch", "--export-root", root), "unknown campaign 'nosuch'"),
            (("bind-records", "pine", "--export-root", empty), "has no current records export"),
            (("bind-records", "pine", "--export-root", self.base / "missing"), "symlink, missing or not a directory"),
            (("bind-records", "pine", "--export-root", link), "symlink, missing or not a directory"),
            (("bind-records", "pine", "--export-root", root), "two campaigns are bound to the same records project"),
            (("bind-records", "pine", "--export-root", self.base / "cedar"), "has no current records export"),
            (("unbind-records", "nosuch"), "unknown campaign 'nosuch'"),
        ]
        for arguments, rule in cases:
            with self.subTest(arguments[:2], rule=rule):
                err = self.refused(*arguments, rule=rule)
                self.assertNotIn(pin.source_project_id, err)
                self.assertNotIn(str(self.base), err)

    def test_unusable_manifest_and_overlap_are_refused(self):
        root, pin = self.export("exports-cedar")
        manifest = next((root / "exports").iterdir()) / "EXPORT.json"
        good = manifest.read_bytes()
        os.chmod(manifest.parent, 0o700)
        os.chmod(manifest, 0o600)
        bad = json.loads(good)
        bad["source_project_id"] = "NOT-HEX-" + "0" * 8
        manifest.write_text(json.dumps(bad), encoding="utf-8")
        self.refused("bind-records", "cedar", "--export-root", root, rule="not a private, readable records export")
        manifest.write_bytes(good)
        os.chmod(root, 0o755)  # not private: read_export would never read it either
        self.refused("bind-records", "cedar", "--export-root", root, rule="not a private, readable records export")
        os.chmod(root, 0o700)
        inside, _ = real_export(self.base / "pine", self.made)  # an export root inside ANOTHER campaign's directory
        self.refused("bind-records", "cedar", "--export-root", inside, rule="overlaps another campaign's directory")

    def test_symlinked_parent_folder_is_refused_before_any_read(self):
        root, pin = self.export("exports-cedar")
        via = self.base / "linked-parent"
        via.symlink_to(self.base)  # the export folder itself is real; one of its parents is a link
        with mock.patch.object(campaign_summary, "project_id", side_effect=AssertionError("manifest was read")):
            err = self.refused("bind-records", "cedar", "--export-root", via / root.relative_to(self.base),
                               rule="goes through a symlinked folder; give its real path")
        self.assertNotIn(str(self.base), err)
        self.assertNotIn("records", self.entries()[0])

    def test_register_io_errors_give_a_fixed_path_free_refusal(self):
        root, pin = self.export("exports-cedar")
        self.assertEqual(self.run_cli("campaigns", "bind-records", "cedar", "--export-root", root)[0], 0)
        before = self.register.read_bytes()
        blocker = self.base / "not-a-folder"
        blocker.write_text("synthetic", encoding="utf-8")
        with mock.patch.dict(os.environ, {registry.ENV: str(blocker / "campaigns.json")}):
            for arguments in (("bind-records", "pine", "--export-root", root), ("unbind-records", "cedar")):
                with self.subTest(arguments[0]):
                    code, out, err = self.run_cli("campaigns", *arguments)
                    self.assertEqual((code, out), (1, ""), err)
                    self.assertEqual(err, "Stopped: the campaign register could not be read or written\n")
                    self.assertNotIn(str(self.base), err)
        self.assertEqual(self.register.read_bytes(), before)

    def test_project_id_matches_read_export(self):
        root, pin = self.export("exports-cedar")
        self.assertEqual(campaign_summary.project_id(str(root)), pin.source_project_id)
        state = campaign_summary.read_export({"export_root": str(root), "source_project_id": pin.source_project_id},
                                             now=self.made + timedelta(hours=1))
        self.assertEqual(state["state"], "ok")
        with self.assertRaisesRegex(ValueError, "no current records export"):
            campaign_summary.project_id(str(self.base / "no-such-root"))


if __name__ == "__main__":
    unittest.main()
