"""DF1125-03: `backup --private-work` carries finding drafts, review receipts, review assignments and the reviewer
roster, in encrypted archives only (synthetic data; keys made at run time; nothing is sent).

Run from the repository root: python3 -B -m unittest tests.test_backup_private_work -v
"""
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import backup  # noqa: E402
from tests.test_backup_encrypt import GPG, make_home  # noqa: E402

DRAFT = "fnd_0123456789ab"
WORK = {f"private/drafts/{DRAFT}.json": b'{"id": "fnd_0123456789ab", "claim": "Synthetic claim."}\n',
        f"private/reviews/{DRAFT}.json": b'[{"role": "fact", "reviewer": "Synthetic Reviewer"}]\n',
        "private/reviews/binding-log.jsonl": b'{"synthetic": true}\n',
        f"private/review-assignments/{DRAFT}.json": b'{"fact": {"reviewer": "Synthetic Reviewer"}}\n',
        "private/reviewers.json": b'{"reviewers": [{"name": "Synthetic Reviewer"}]}\n'}
MARKER = "synthetic-secret-name"


def run_cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def write(root, files):
    for name, data in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 28, 12, 0, 0, tzinfo=timezone.utc)


@unittest.skipUnless(GPG, "gpg is not installed")
class PrivateWorkBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="synthetic-df112503-")
        cls.base = Path(cls.tmp.name)
        cls.home, cls.pub, _ = make_home(cls.base, "organizer")
        cls.root = cls.base / "campaign"
        r = run_cli("init", "--directory", cls.root, "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        assert r.returncode == 0, r.stderr
        (cls.base / "policy.txt").write_bytes(b"Synthetic DF1125-03 policy text.\n")
        r = run_cli("ingest", "--directory", cls.root, "--file", cls.base / "policy.txt", "--source-id", "demo-1")
        assert r.returncode == 0, r.stderr
        write(cls.root, WORK)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.home), "--kill", "all"], capture_output=True, timeout=30)
        shutil.rmtree(cls.home, ignore_errors=True)
        cls.tmp.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="synthetic-df112503-case-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.out = self.work / "out"
        self.out.mkdir()

    def copy(self, extra=None):
        """A private copy of the campaign for a case that changes files."""
        root = self.work / "campaign"
        shutil.copytree(self.root, root, symlinks=True)
        write(root, extra or {})
        return root

    def test_encrypted_archive_carries_and_restores_all_four(self):
        archive = self.out / "work.tar.gpg"
        manifest = backup.export(self.root, archive, encrypt_to=self.pub, private_work=True)
        self.assertTrue(manifest["encrypted"])
        for name, data in WORK.items():
            self.assertEqual(manifest["files"][name], {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        self.assertNotIn(b"Synthetic Reviewer", archive.read_bytes())
        report = backup.verify(archive, expected_manifest_sha256=manifest["manifest_sha256"], gnupg_home=self.home)
        self.assertTrue(report["ok"])
        target = self.work / "restored"
        backup.restore(archive, target, gnupg_home=self.home)
        for name, data in WORK.items():
            self.assertEqual((target / name).read_bytes(), data, name)
            self.assertEqual(stat.S_IMODE((target / name).stat().st_mode), 0o600, name)
        for folder in ("private/drafts", "private/reviews", "private/review-assignments"):
            self.assertEqual(stat.S_IMODE((target / folder).stat().st_mode), 0o700, folder)
        # a second restore never writes over the first one's files
        with self.assertRaisesRegex(backup.BackupError, "not an empty directory"):
            backup.restore(archive, target, gnupg_home=self.home)
        self.assertEqual((target / "private/reviewers.json").read_bytes(), WORK["private/reviewers.json"])

    def test_drill_list_and_diff_accept_the_new_archive(self):
        archive = self.out / "work.tar.gpg"
        backup.export(self.root, archive, encrypt_to=self.pub, private_work=True)
        receipt = backup.drill(archive, gnupg_home=self.home, tmp_parent=self.work)
        self.assertTrue(receipt["passed"], receipt["steps"])
        listed = backup.list_archives(self.out, gnupg_home=self.home)
        self.assertEqual([(a["name"], a["verify"]) for a in listed["archives"]], [("work.tar.gpg", "ok")])
        self.assertTrue(backup.diff(archive, self.root, gnupg_home=self.home)["current"])
        changed = self.copy({f"private/drafts/{DRAFT}.json": b'{"id": "fnd_0123456789ab", "claim": "Edited."}\n'})
        report = backup.diff(archive, changed, gnupg_home=self.home)
        self.assertEqual(report["changed"], [f"private/drafts/{DRAFT}.json"])
        # an archive made without the flag still reports nothing about private work
        plain = self.out / "plain.tar"
        backup.export(self.root, plain)
        self.assertTrue(backup.diff(plain, changed)["current"])

    def test_flag_without_a_key_is_refused_with_nothing_written(self):
        with self.assertRaisesRegex(backup.BackupError, "private_work: needs an encryption key"):
            backup.export(self.root, self.out / "plain.tar", private_work=True)
        r = run_cli("backup", "--directory", self.root, "--out", self.out / "cli.tar", "--private-work")
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs an encryption key", r.stderr)
        r = run_cli("backup", "--directory", self.root, "--into", self.out, "--private-work")
        self.assertEqual(r.returncode, 1)
        self.assertIn("backup failed: private_work: needs an encryption key", r.stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_cli_with_a_key_writes_the_members(self):
        r = run_cli("backup", "--directory", self.root, "--out", self.out / "cli.tar.gpg", "--encrypt-to", self.pub,
                    "--private-work")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = backup.verify(self.out / "cli.tar.gpg", gnupg_home=self.home)
        self.assertEqual(report["files"], len(backup.export(self.root, self.out / "x.tar")["files"]) + len(WORK))

    def test_unsafe_entries_refuse_the_whole_backup(self):
        receipt = f"private/reviews/{DRAFT}.json"
        cases = {
            "symlinked review receipt": (lambda r: (r / receipt).unlink() or os.symlink(r / "campaign.json", r / receipt),
                                         f"{receipt} is not a regular file"),
            "unexpected file under drafts": (lambda r: write(r, {f"private/drafts/{MARKER}.txt": b"x"}),
                                             "unexpected name under private/drafts/"),
            "folder under drafts": (lambda r: (r / "private/drafts" / MARKER).mkdir(),
                                    "unexpected name under private/drafts/"),
            "symlinked assignments folder": (lambda r: shutil.rmtree(r / "private/review-assignments") or os.symlink(
                r / "private/drafts", r / "private/review-assignments"), "private/review-assignments must be a real folder"),
            "roster is a folder": (lambda r: (r / "private/reviewers.json").unlink() or (r / "private/reviewers.json").mkdir(),
                                   "private/reviewers.json is not a regular file"),
            "fifo in reviews": (lambda r: os.mkfifo(r / "private/reviews" / f"{DRAFT}x.json"),
                                f"private/reviews/{DRAFT}x.json is not a regular file"),
        }
        for label, (damage, message) in cases.items():
            with self.subTest(case=label):
                root = self.work / ("c-" + label.replace(" ", "-"))
                shutil.copytree(self.root, root, symlinks=True)
                damage(root)
                with self.assertRaises(backup.BackupError) as raised:
                    backup.export(root, self.out / "work.tar.gpg", encrypt_to=self.pub, private_work=True)
                self.assertIn(message, str(raised.exception))
                self.assertIn("nothing was written", str(raised.exception))
                self.assertNotIn(str(self.work), str(raised.exception))
                self.assertNotIn(MARKER, str(raised.exception))
                self.assertEqual(list(self.out.iterdir()), [])
                # without the flag the same campaign still backs up as before
                backup.export(root, self.out / "plain.tar")
                (self.out / "plain.tar").unlink()

    def test_without_the_flag_the_archive_is_byte_identical(self):
        root = self.work / "small"
        write(root, {"campaign.json": b'{"name": "Synthetic"}\n', "kit/notes.md": b"synthetic\n"})
        os.utime(root / "campaign.json", (1_700_000_000, 1_700_000_000))
        os.utime(root / "kit/notes.md", (1_700_000_000, 1_700_000_000))
        with patch.object(backup, "datetime", _Frozen):
            before = backup.export(root, self.out / "before.tar")
            write(root, WORK)
            after = backup.export(root, self.out / "after.tar")
            explicit = backup.export(root, self.out / "explicit.tar", private_work=False)
        self.assertEqual((self.out / "before.tar").read_bytes(), (self.out / "after.tar").read_bytes())
        self.assertEqual((self.out / "after.tar").read_bytes(), (self.out / "explicit.tar").read_bytes())
        self.assertEqual(before["manifest_sha256"], after["manifest_sha256"])
        self.assertFalse(any(name.startswith(("private/drafts", "private/review")) for name in after["files"]))

    def test_other_private_names_are_still_refused_by_the_readers(self):
        def crafted(name):
            entries = {"campaign.json": b"{}", name: b"synthetic"}
            files = {n: {"sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)} for n, d in entries.items()}
            manifest = {"schema_version": 1, "engine_version": "synthetic", "created_at": "2026-01-01T00:00:00+00:00",
                        "source": {"kind": "local", "root_name": "campaign"}, "files": files,
                        "counts": {"files": 2, "objects": 0, "receipts": 0, "bytes": sum(len(d) for d in entries.values())}}
            path = self.out / "crafted.tar"
            path.unlink(missing_ok=True)
            with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
                for member, data in [(backup.MANIFEST, json.dumps(manifest).encode()), *entries.items()]:
                    info = tarfile.TarInfo(member)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            return path

        for name in ("private/withdrawn/x.json", "private/drafts/sub/x.json", "private/drafts/x.txt",
                     "private/drafts", "private/reviews/other.jsonl", "private/reviewers.json.bak",
                     "private/publication-log.jsonl", "private/review-assignments/" + "a" * 65 + ".json"):
            with self.subTest(member=name):
                path = crafted(name)
                with self.assertRaisesRegex(backup.BackupError, "unsupported_member_path"):
                    backup.verify(path)
                with self.assertRaisesRegex(backup.BackupError, "unsupported_member_path"):
                    backup.restore(path, self.work / "never")
                self.assertFalse(os.path.lexists(self.work / "never"))
        for name in WORK:
            with self.subTest(allowed=name):
                self.assertTrue(backup.verify(crafted(name))["ok"])


if __name__ == "__main__":
    unittest.main()
