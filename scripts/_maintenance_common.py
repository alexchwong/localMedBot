"""Shared maintenance/release primitives for localMedBot.

This module intentionally depends only on the Python standard library and Git.
"""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


class ToolError(Exception):
    def __init__(self, code: str, detail: str = "", **fields):
        self.code = code
        self.detail = detail
        self.fields = {k: v for k, v in fields.items() if v is not None}
        super().__init__(code)

    def document(self) -> dict:
        error = {"code": self.code}
        if self.detail:
            error["detail"] = self.detail
        error.update(self.fields)
        return {"error": error}


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    sha: str
    path: str


HISTORICAL_PATHS = {
    "docs/implementation-plan.md",
    "docs/verification.md",
    "docs/acceptance-0.1.0.md",
    "docs/acceptance-0.1.0.json",
    "docs/live-environment-check.json",
    "docs/self-step-verification-0.1.0.json",
    "docs/test-results.json",
}

_REQUIRED_SOURCE_PATHS = {
    "src/localmedbot/version.json",
    "src/localmedbot/_version.py",
    "src/localmedbot/__init__.py",
    "pyproject.toml",
}


def checkout_root(script_file: str | Path) -> Path:
    return Path(script_file).resolve().parents[1]


def ensure_environment(root: Path) -> None:
    expected = (root / ".env").resolve()
    if Path(sys.prefix).resolve() != expected:
        raise ToolError("environment_required", detail="run this command using the checkout .env")


def _run_git(root: Path, args: list[str], *, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ToolError("git_unavailable") from exc
    if check and proc.returncode != 0:
        raise ToolError("git_failed", detail="git command failed")
    return proc


def resolve_commit(root: Path, ref: str) -> str:
    proc = _run_git(root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    if proc.returncode != 0:
        raise ToolError("ref_invalid", ref=ref)
    try:
        value = proc.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ToolError("ref_invalid", ref=ref) from exc
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise ToolError("ref_invalid", ref=ref)
    return value.lower()


def head_commit(root: Path) -> str:
    return resolve_commit(root, "HEAD")


def require_head(root: Path, commit: str) -> None:
    if commit != head_commit(root):
        raise ToolError("ref_not_head", ref=commit)


def tracked_dirty(root: Path) -> bool:
    unstaged = _run_git(root, ["diff", "--quiet", "--"], check=False).returncode
    staged = _run_git(root, ["diff", "--cached", "--quiet", "HEAD", "--"], check=False).returncode
    if unstaged not in (0, 1) or staged not in (0, 1):
        raise ToolError("git_failed", detail="unable to inspect tracked state")
    return bool(unstaged or staged)


def require_clean_tracked(root: Path) -> None:
    if tracked_dirty(root):
        raise ToolError("tracked_tree_dirty")


def _decode_paths(raw: bytes, *, error_code: str) -> list[str]:
    if not raw:
        return []
    parts = raw.split(b"\0")
    if parts[-1] == b"":
        parts.pop()
    out: list[str] = []
    for item in parts:
        try:
            path = item.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(error_code) from exc
        if not valid_repo_path(path):
            raise ToolError(error_code, path=path)
        out.append(path)
    return out


def head_index_paths(root: Path) -> set[str]:
    head = _decode_paths(_run_git(root, ["ls-tree", "-r", "-z", "--name-only", "HEAD"]).stdout, error_code="tree_path_invalid")
    index = _decode_paths(_run_git(root, ["ls-files", "-z", "--cached"]).stdout, error_code="index_path_invalid")
    return set(head) | set(index)


def index_differs(root: Path) -> bool:
    staged = _run_git(root, ["diff", "--cached", "--quiet", "HEAD", "--"], check=False).returncode
    unstaged = _run_git(root, ["diff", "--quiet", "--"], check=False).returncode
    if staged not in (0, 1) or unstaged not in (0, 1):
        raise ToolError("git_failed", detail="unable to compare index state")
    return bool(staged or unstaged)


def index_paths(root: Path) -> list[str]:
    return _decode_paths(_run_git(root, ["ls-files", "-z", "--cached"]).stdout, error_code="index_path_invalid")


def read_index_file(root: Path, path: str) -> bytes:
    if not valid_repo_path(path):
        raise ToolError("index_path_invalid", path=path)
    proc = _run_git(root, ["show", f":{path}"], check=False)
    if proc.returncode != 0:
        raise ToolError("version_representation_missing", path=path, detail="state=index")
    return proc.stdout


def list_tree(root: Path, commit: str) -> dict[str, TreeEntry]:
    raw = _run_git(root, ["ls-tree", "-r", "-z", "--full-tree", commit]).stdout
    entries: dict[str, TreeEntry] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            meta, path_b = record.split(b"\t", 1)
            mode_b, typ_b, sha_b = meta.split(b" ", 2)
            path = path_b.decode("utf-8")
            mode = mode_b.decode("ascii")
            obj_type = typ_b.decode("ascii")
            sha = sha_b.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolError("tree_path_invalid") from exc
        if not valid_repo_path(path):
            raise ToolError("tree_path_invalid", path=path)
        entries[path] = TreeEntry(mode, obj_type, sha, path)
    return entries


def read_blob(root: Path, sha: str) -> bytes:
    proc = _run_git(root, ["cat-file", "blob", sha], check=False)
    if proc.returncode != 0:
        raise ToolError("git_blob_unreadable")
    return proc.stdout


def read_tree_file(root: Path, tree: dict[str, TreeEntry], path: str) -> bytes:
    entry = tree.get(path)
    if entry is None or entry.object_type != "blob":
        raise ToolError("version_representation_missing", path=path, detail="state=commit")
    return read_blob(root, entry.sha)


def valid_repo_path(path: str) -> bool:
    if not path or "\\" in path or path.startswith("/") or "\0" in path:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def hygiene_reason(path: str) -> str | None:
    if not valid_repo_path(path):
        return "unsafe_path"
    if path in HISTORICAL_PATHS:
        return "historical_artifact"
    parts = path.split("/")
    base = parts[-1]
    lowered = base.lower()
    for part in parts:
        if part in {".env", ".venv", "__pycache__", "build", "dist", "test-results", "playwright-report", ".pytest_cache", "htmlcov"}:
            return "generated_or_private"
        if part.startswith(".env.") or part.startswith(".localmedbot") or part.endswith(".egg-info"):
            return "generated_or_private"
        if part == ".coverage":
            return "generated_or_private"
    if any(parts[i] == "fixtures" and parts[i + 1] == "scratch" for i in range(len(parts) - 1)):
        return "scratch_fixture"
    if fnmatch.fnmatch(lowered, "*.pem") or fnmatch.fnmatch(lowered, "*.key") or fnmatch.fnmatch(lowered, "*.p12") or fnmatch.fnmatch(lowered, "*.pfx"):
        return "private_key_container"
    if fnmatch.fnmatch(lowered, "*.sqlite*") or fnmatch.fnmatch(lowered, "*.db") or lowered.endswith("-wal") or lowered.endswith("-shm"):
        return "runtime_database"
    if lowered.endswith(".pyc"):
        return "generated_or_private"
    if len(parts) >= 2 and parts[0] == "docs" and lowered.endswith(".json"):
        return "generated_docs_evidence"
    return None


def check_hygiene_paths(paths: Iterable[str], *, state: str) -> None:
    for path in sorted(set(paths)):
        reason = hygiene_reason(path)
        if reason:
            raise ToolError("hygiene_prohibited", path=path, detail=f"state={state};reason={reason}")


def check_head_index_hygiene(root: Path) -> None:
    check_hygiene_paths(head_index_paths(root), state="head_or_index")


def check_tree_hygiene(tree: dict[str, TreeEntry]) -> None:
    check_hygiene_paths(tree, state="commit")


def load_version_helper(root: Path):
    path = root / "src" / "localmedbot" / "_version.py"
    if not path.is_file():
        raise ToolError("version_helper_missing", path=str(path.relative_to(root)))
    spec = importlib.util.spec_from_file_location("_localmedbot_version_helper", path)
    if spec is None or spec.loader is None:
        raise ToolError("version_helper_invalid")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ToolError("version_helper_invalid") from exc
    return module


def parse_version_bytes(root: Path, data: bytes, *, state: str):
    helper = load_version_helper(root)
    try:
        return helper.parse_version_metadata(data)
    except Exception as exc:
        raise ToolError("version_metadata_invalid", detail=f"state={state}") from exc


def _decode_text(data: bytes, path: str, state: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("version_representation_invalid", path=path, detail=f"state={state}") from exc


def application_has_product_version(data: bytes, path: str, state: str) -> bool:
    text = _decode_text(data, path, state)
    return re.search(r"(?m)^version:\s*[^#\s]+", text) is not None


def _pyproject_wiring(data: bytes, state: str) -> None:
    path = "pyproject.toml"
    text = _decode_text(data, path, state)
    project_match = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    if project_match is None:
        raise ToolError("version_wiring_invalid", path=path, detail=f"state={state}")
    project = project_match.group(1)
    if re.search(r"(?m)^version\s*=", project):
        raise ToolError("version_wiring_invalid", path=path, detail=f"state={state};static_version")
    if re.search(r"(?m)^dynamic\s*=\s*\[\s*[\"']version[\"']\s*\]\s*$", project) is None:
        raise ToolError("version_wiring_invalid", path=path, detail=f"state={state};dynamic_version")
    dyn = re.search(r"(?ms)^\[tool\.setuptools\.dynamic\]\s*(.*?)(?=^\[|\Z)", text)
    if dyn is None or re.search(r"(?m)^version\s*=\s*\{\s*attr\s*=\s*[\"']localmedbot\.__version__[\"']\s*\}\s*$", dyn.group(1)) is None:
        raise ToolError("version_wiring_invalid", path=path, detail=f"state={state};setuptools_dynamic")
    package_data = re.search(r"(?ms)^\[tool\.setuptools\.package-data\]\s*(.*?)(?=^\[|\Z)", text)
    if package_data is None or "version.json" not in package_data.group(1):
        raise ToolError("version_wiring_invalid", path=path, detail=f"state={state};package_data")


def _init_wiring(data: bytes, state: str) -> None:
    path = "src/localmedbot/__init__.py"
    text = _decode_text(data, path, state)
    if "from ._version import load_version_metadata" not in text or '__version__ = load_version_metadata()["version"]' not in text:
        raise ToolError("version_wiring_invalid", path=path, detail=f"state={state}")


def check_source_version_state(
    root: Path,
    reader: Callable[[str], bytes],
    paths: Iterable[str],
    *,
    state: str,
) -> dict:
    path_set = set(paths)
    for required in _REQUIRED_SOURCE_PATHS:
        if required not in path_set:
            raise ToolError("version_representation_missing", path=required, detail=f"state={state}")
    # The loader is part of the version-derivation wiring even though metadata
    # parsing below deliberately uses the current standard-library-only helper.
    reader("src/localmedbot/_version.py")
    metadata = parse_version_bytes(root, reader("src/localmedbot/version.json"), state=state)
    _pyproject_wiring(reader("pyproject.toml"), state)
    _init_wiring(reader("src/localmedbot/__init__.py"), state)
    app_paths = sorted(
        path for path in path_set
        if path.startswith("applications/") and path.endswith("/application.yaml") and len(path.split("/")) == 3
    )
    if not app_paths:
        raise ToolError("version_representation_missing", path="applications/*/application.yaml", detail=f"state={state}")
    for path in app_paths:
        if application_has_product_version(reader(path), path, state):
            raise ToolError("version_wiring_invalid", path=path, detail=f"state={state};duplicated_product_version")
    return metadata


def working_paths(root: Path) -> list[str]:
    required = list(_REQUIRED_SOURCE_PATHS)
    required.extend(str(path.relative_to(root)).replace(os.sep, "/") for path in (root / "applications").glob("*/application.yaml"))
    return sorted(set(required))


def check_working_source_state(root: Path) -> dict:
    paths = working_paths(root)
    def reader(path: str) -> bytes:
        target = root / PurePosixPath(path)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise ToolError("version_representation_missing", path=path, detail="state=working") from exc
    return check_source_version_state(root, reader, paths, state="working")


def check_index_source_state(root: Path) -> dict:
    paths = index_paths(root)
    return check_source_version_state(root, lambda p: read_index_file(root, p), paths, state="index")


def check_commit_source_state(root: Path, tree: dict[str, TreeEntry]) -> dict:
    return check_source_version_state(root, lambda p: read_tree_file(root, tree, p), tree.keys(), state="commit")


def check_installed_version(root: Path, expected: str) -> None:
    try:
        import importlib.metadata
        import localmedbot
        installed = importlib.metadata.version("localMedBot")
    except Exception as exc:
        raise ToolError("installed_version_unavailable", detail="state=working") from exc
    if installed != expected:
        raise ToolError("version_mismatch", detail="state=working;representation=installed_distribution")
    if getattr(localmedbot, "__version__", None) != expected:
        raise ToolError("version_mismatch", detail="state=working;representation=runtime")

    proc = subprocess.run(
        [sys.executable, "-m", "localmedbot.cli", "check"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise ToolError("cli_check_failed", detail="state=working")
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("cli_check_failed", detail="state=working;invalid_json") from exc
    if result.get("version") != expected:
        raise ToolError("version_mismatch", detail="state=working;representation=cli")


def check_maintenance(root: Path) -> dict:
    ensure_environment(root)
    metadata = check_working_source_state(root)
    check_installed_version(root, metadata["version"])
    if index_differs(root):
        check_index_source_state(root)
    check_head_index_hygiene(root)
    return metadata


def json_success(document: dict) -> int:
    sys.stdout.write(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def json_failure(exc: ToolError) -> int:
    sys.stderr.write(json.dumps(exc.document(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 1
