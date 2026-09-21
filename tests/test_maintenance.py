from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import _maintenance_common as common


class MaintenanceTests(unittest.TestCase):
    def test_version_metadata_contract(self):
        helper = common.load_version_helper(ROOT)
        valid = helper.parse_version_metadata(b'{"version":"0.1.1","releaseable":false}')
        self.assertEqual(valid, {"version": "0.1.1", "releaseable": False})
        bad = [
            b'{"version":"01.1.1","releaseable":false}',
            b'{"version":"0.1","releaseable":false}',
            b'{"version":"0.1.1","releaseable":"false"}',
            b'{"version":"0.1.1"}',
            b'{"version":"0.1.1","releaseable":false,"extra":1}',
            b'{"version":"0.1.1","releaseable":false,"releaseable":true}',
            b'not-json',
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(Exception):
                helper.parse_version_metadata(raw)

    def test_environment_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env").mkdir()
            with mock.patch.object(common.sys, "prefix", str(root / ".env")):
                common.ensure_environment(root)
            with mock.patch.object(common.sys, "prefix", str(root / "elsewhere")):
                with self.assertRaises(common.ToolError) as ctx:
                    common.ensure_environment(root)
                self.assertEqual(ctx.exception.code, "environment_required")

    def test_hygiene_rules_are_targeted(self):
        rejected = [
            ".env/pyvenv.cfg",
            "nested/.env.local",
            "state/.localmedbot-test/data.db",
            "custom-data/fixtures/scratch/item.json",
            "x/__pycache__/a.pyc",
            "docs/result.json",
            "secrets/client.pem",
            "build/output.txt",
        ]
        for path in rejected:
            with self.subTest(path=path):
                self.assertIsNotNone(common.hygiene_reason(path))
        allowed = [
            "tests/fixtures/steps/a.json",
            "guideline_sets/demo/devel/snapshot.json",
            "docs/versioning.md",
            "applications/example/schema.json",
            "some/fixtures/reviewed/item.json",
        ]
        for path in allowed:
            with self.subTest(path=path):
                self.assertIsNone(common.hygiene_reason(path))

    def _init_version_repo(self, root: Path):
        (root / "src/localmedbot").mkdir(parents=True)
        (root / "applications/a").mkdir(parents=True)
        shutil.copy2(ROOT / "src/localmedbot/_version.py", root / "src/localmedbot/_version.py")
        (root / "src/localmedbot/version.json").write_text('{"version":"0.1.1","releaseable":false}\n', encoding="utf-8")
        (root / "src/localmedbot/__init__.py").write_text(
            'from ._version import load_version_metadata\n__version__ = load_version_metadata()["version"]\n', encoding="utf-8"
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname="localMedBot"\ndynamic = ["version"]\n\n'
            '[tool.setuptools.package-data]\nlocalmedbot=["version.json"]\n\n'
            '[tool.setuptools.dynamic]\nversion = {attr = "localmedbot.__version__"}\n', encoding="utf-8"
        )
        (root / "applications/a/application.yaml").write_text("id: a\nversion: 0.1.1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)

    def test_index_inconsistency_not_hidden_by_working_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_version_repo(root)
            app = root / "applications/a/application.yaml"
            app.write_text("id: a\nversion: 9.9.9\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", str(app.relative_to(root))], check=True)
            app.write_text("id: a\nversion: 0.1.1\n", encoding="utf-8")
            self.assertEqual(common.check_working_source_state(root)["version"], "0.1.1")
            with self.assertRaises(common.ToolError) as ctx:
                common.check_index_source_state(root)
            self.assertEqual(ctx.exception.code, "version_mismatch")
            self.assertIn("state=index", ctx.exception.detail)

    def test_working_inconsistency_not_hidden_by_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_version_repo(root)
            app = root / "applications/a/application.yaml"
            app.write_text("id: a\nversion: 0.1.1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", str(app.relative_to(root))], check=True)
            app.write_text("id: a\nversion: 9.9.9\n", encoding="utf-8")
            self.assertEqual(common.check_index_source_state(root)["version"], "0.1.1")
            with self.assertRaises(common.ToolError) as ctx:
                common.check_working_source_state(root)
            self.assertEqual(ctx.exception.code, "version_mismatch")
            self.assertIn("state=working", ctx.exception.detail)

    def test_version_loader_is_required_in_source_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_version_repo(root)
            (root / "src/localmedbot/_version.py").unlink()
            with self.assertRaises(common.ToolError) as ctx:
                common.check_working_source_state(root)
            self.assertEqual(ctx.exception.code, "version_representation_missing")
            self.assertEqual(ctx.exception.path if hasattr(ctx.exception, "path") else ctx.exception.fields.get("path"), "src/localmedbot/_version.py")

    def test_command_protocol_helpers(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = common.json_success({"status": "ok", "version": "0.1.1"})
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(json.loads(out.getvalue()), {"status": "ok", "version": "0.1.1"})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = common.json_failure(common.ToolError("synthetic_error", detail="safe"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "synthetic_error")


if __name__ == "__main__":
    unittest.main()
