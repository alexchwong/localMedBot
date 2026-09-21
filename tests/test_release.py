from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import _maintenance_common as common
import release


class ReleaseTests(unittest.TestCase):
    def _git(self, root: Path, *args: str, check: bool = True):
        return subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def _repo(self, root: Path, *, releaseable: bool = False):
        for directory in ["src/localmedbot", "applications/a", "payload"]:
            (root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "src/localmedbot/_version.py", root / "src/localmedbot/_version.py")
        (root / "src/localmedbot/version.json").write_text(
            json.dumps({"version": "0.1.1", "releaseable": releaseable}) + "\n", encoding="utf-8"
        )
        (root / "src/localmedbot/__init__.py").write_text(
            'from ._version import load_version_metadata\n__version__ = load_version_metadata()["version"]\n', encoding="utf-8"
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname="localMedBot"\ndynamic = ["version"]\n\n'
            '[tool.setuptools.package-data]\nlocalmedbot=["version.json"]\n\n'
            '[tool.setuptools.dynamic]\nversion = {attr = "localmedbot.__version__"}\n', encoding="utf-8"
        )
        (root / "applications/a/application.yaml").write_text("id: a\nversion: 0.1.1\n", encoding="utf-8")
        (root / "release-manifest.txt").write_text(
            "release-manifest.txt\npyproject.toml\nsrc/localmedbot/**\napplications/**\npayload/**\n", encoding="utf-8"
        )
        (root / "payload/plain.txt").write_text("plain\n", encoding="utf-8")
        (root / "payload/café.txt").write_text("utf8\n", encoding="utf-8")
        executable = root / "payload/run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "test@example.invalid")
        self._git(root, "config", "user.name", "Test")
        self._git(root, "add", ".")
        self._git(root, "commit", "-qm", "baseline")
        return common.head_commit(root)

    def test_manifest_resolution_excludes_untracked_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root)
            (root / "payload/untracked.txt").write_text("no\n", encoding="utf-8")
            tree = common.list_tree(root, commit)
            payload = release.resolve_payload(root, tree)
            self.assertIn("payload/plain.txt", payload)
            self.assertNotIn("payload/untracked.txt", payload)
            self.assertEqual(len(payload), len(set(payload)))

    def test_manifest_unsafe_and_unmatched_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            for text in ["missing.txt\n", "../escape\n", "payload/*.txt\n"]:
                with self.subTest(text=text):
                    (root / "release-manifest.txt").write_text(text, encoding="utf-8")
                    self._git(root, "add", "release-manifest.txt")
                    self._git(root, "commit", "-qm", "manifest case")
                    commit = common.head_commit(root)
                    with self.assertRaises(common.ToolError):
                        release.resolve_payload(root, common.list_tree(root, commit))

    def test_archive_is_byte_deterministic_and_canonical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root)
            metadata, payload = release.validate_snapshot(root, commit)
            a = root / "a.zip"
            b = root / "b.zip"
            release._canonical_archive(root, commit, a, metadata, payload)
            release._canonical_archive(root, commit, b, metadata, payload)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            release.verify_archive(root, commit, a)
            with zipfile.ZipFile(a) as zf:
                infos = zf.infolist()
                self.assertTrue(infos)
                self.assertTrue(all(i.flag_bits == 0x0800 for i in infos))
                by_name = {i.filename: i for i in infos}
                self.assertEqual(by_name["localMedBot-0.1.1/payload/run.sh"].external_attr, 0o100755 << 16)
                self.assertEqual(by_name["localMedBot-0.1.1/payload/plain.txt"].external_attr, 0o100644 << 16)
                self.assertIn("localMedBot-0.1.1/payload/café.txt", by_name)

    def test_noncanonical_or_altered_archive_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root)
            metadata, payload = release.validate_snapshot(root, commit)
            prefix = "localMedBot-0.1.1/"
            bad = root / "bad.zip"
            with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_STORED) as zf:
                for path, (_, data) in payload.items():
                    zf.writestr(prefix + path, data)
            with self.assertRaises(common.ToolError) as ctx:
                release.verify_archive(root, commit, bad)
            self.assertIn(ctx.exception.code, {"archive_metadata_invalid", "archive_corrupt"})

            altered = root / "altered.zip"
            release._canonical_archive(root, commit, altered, metadata, payload)
            with zipfile.ZipFile(altered, "a") as zf:
                zf.writestr(release._zip_info(prefix + "payload/plain.txt", "100644"), b"changed")
            with self.assertRaises(common.ToolError):
                release.verify_archive(root, commit, altered)

    def test_dirty_state_and_committed_snapshot_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root)
            original = release.validate_snapshot(root, commit)[1]["payload/plain.txt"][1]
            (root / "payload/plain.txt").write_text("unstaged\n", encoding="utf-8")
            with self.assertRaises(common.ToolError) as ctx:
                release.validate_command(root, commit)
            self.assertEqual(ctx.exception.code, "tracked_tree_dirty")
            self.assertEqual(release.validate_snapshot(root, commit)[1]["payload/plain.txt"][1], original)
            self._git(root, "add", "payload/plain.txt")
            self.assertEqual(release.validate_snapshot(root, commit)[1]["payload/plain.txt"][1], original)

    def test_prohibited_tracked_file_outside_manifest_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            bad = root / "private" / "client.key"
            bad.parent.mkdir()
            bad.write_text("synthetic-not-a-key\n", encoding="utf-8")
            self._git(root, "add", "private/client.key")
            self._git(root, "commit", "-qm", "prohibited path")
            commit = common.head_commit(root)
            with self.assertRaises(common.ToolError) as ctx:
                release.validate_snapshot(root, commit)
            self.assertEqual(ctx.exception.code, "hygiene_prohibited")

    def test_matched_symlink_and_gitlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            link = root / "payload/link"
            try:
                link.symlink_to("plain.txt")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            self._git(root, "add", "payload/link")
            self._git(root, "commit", "-qm", "symlink")
            with self.assertRaises(common.ToolError) as ctx:
                release.validate_snapshot(root, common.head_commit(root))
            self.assertEqual(ctx.exception.code, "manifest_object_invalid")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root)
            self._git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},payload/gitlink")
            self._git(root, "commit", "-qm", "gitlink")
            with self.assertRaises(common.ToolError) as ctx:
                release.validate_snapshot(root, common.head_commit(root))
            self.assertEqual(ctx.exception.code, "manifest_object_invalid")

    def test_archive_member_set_and_corruption_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root)
            metadata, payload = release.validate_snapshot(root, commit)
            good = root / "good.zip"
            release._canonical_archive(root, commit, good, metadata, payload)
            raw = good.read_bytes()
            corrupt = root / "corrupt.zip"
            corrupt.write_bytes(raw[:-8])
            with self.assertRaises(common.ToolError) as ctx:
                release.verify_archive(root, commit, corrupt)
            self.assertEqual(ctx.exception.code, "archive_corrupt")

            missing_payload = dict(payload)
            missing_payload.pop("payload/plain.txt")
            missing = root / "missing.zip"
            release._write_canonical_zip(missing, "localMedBot-0.1.1/", missing_payload)
            with self.assertRaises(common.ToolError) as ctx:
                release.verify_archive(root, commit, missing)
            self.assertEqual(ctx.exception.code, "archive_members_mismatch")

    def test_release_permission_false_fails_before_remote(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root, releaseable=False)
            with self.assertRaises(common.ToolError) as ctx:
                release.release_check(root, commit)
            self.assertEqual(ctx.exception.code, "release_not_permitted")

    def test_new_true_version_passes_against_local_bare_remote(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
            root, remote = Path(td), Path(rd) / "remote.git"
            commit = self._repo(root, releaseable=True)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            self._git(root, "remote", "add", "origin", str(remote))
            metadata, payload = release.release_check(root, commit)
            self.assertTrue(metadata["releaseable"])
            self.assertTrue(payload)

    def test_existing_version_identical_and_changed_payload_fail(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
            root, remote = Path(td), Path(rd) / "remote.git"
            commit = self._repo(root, releaseable=True)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            self._git(root, "remote", "add", "origin", str(remote))
            self._git(root, "tag", "v0.1.1", commit)
            with self.assertRaises(common.ToolError) as ctx:
                release.release_check(root, commit)
            self.assertEqual(ctx.exception.code, "version_already_released")

            (root / "payload/plain.txt").write_text("changed\n", encoding="utf-8")
            self._git(root, "add", "payload/plain.txt")
            self._git(root, "commit", "-qm", "changed payload")
            new_commit = common.head_commit(root)
            with self.assertRaises(common.ToolError) as ctx:
                release.release_check(root, new_commit)
            self.assertEqual(ctx.exception.code, "released_payload_changed")

    def test_remote_uncertainty_fails_closed_for_permitted_version(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            commit = self._repo(root, releaseable=True)
            with self.assertRaises(common.ToolError) as ctx:
                release.release_check(root, commit)
            self.assertEqual(ctx.exception.code, "remote_unavailable")


if __name__ == "__main__":
    unittest.main()
