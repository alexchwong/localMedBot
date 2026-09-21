#!/usr/bin/env python3
"""Validate, build and smoke-test the curated localMedBot source ZIP."""
from __future__ import annotations

import argparse
import binascii
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from _maintenance_common import (
    ToolError,
    TreeEntry,
    check_commit_source_state,
    check_tree_hygiene,
    checkout_root,
    ensure_environment,
    head_commit,
    hygiene_reason,
    json_failure,
    json_success,
    list_tree,
    read_blob,
    require_clean_tracked,
    require_head,
    resolve_commit,
    _run_git,
)

_FIXED_DATE = (1980, 1, 1, 0, 0, 0)
_UTF8_FLAG = 0x0800
_ALLOWED_MODES = {"100644", "100755"}
_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_LOCAL_SIGNATURE = b"PK\x03\x04"
_FIXED_DOS_DATE = 33


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError("argument_invalid")


def _manifest_entries(data: bytes) -> list[tuple[str, bool]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("manifest_invalid", path="release-manifest.txt") from exc
    entries: list[tuple[str, bool]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if any(ord(c) < 32 or ord(c) == 127 for c in line) or "\0" in line:
            raise ToolError("manifest_invalid", path="release-manifest.txt", detail=f"line={number}")
        subtree = line.endswith("/**")
        path = line[:-3] if subtree else line
        if not path or path.startswith("/") or "\\" in path:
            raise ToolError("manifest_invalid", path="release-manifest.txt", detail=f"line={number}")
        if any(token in path for token in ("*", "?", "[", "]")):
            raise ToolError("manifest_invalid", path="release-manifest.txt", detail=f"line={number}")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ToolError("manifest_invalid", path="release-manifest.txt", detail=f"line={number}")
        entries.append((path, subtree))
    if not entries:
        raise ToolError("manifest_empty", path="release-manifest.txt")
    return entries


def _regular_entry(entry: TreeEntry) -> bool:
    return entry.object_type == "blob" and entry.mode in _ALLOWED_MODES


def resolve_payload(root: Path, tree: dict[str, TreeEntry]) -> dict[str, tuple[str, bytes]]:
    manifest_entry = tree.get("release-manifest.txt")
    if manifest_entry is None or manifest_entry.object_type != "blob":
        raise ToolError("manifest_missing", path="release-manifest.txt")
    entries = _manifest_entries(read_blob(root, manifest_entry.sha))
    selected: dict[str, TreeEntry] = {}
    for path, subtree in entries:
        if subtree:
            prefix = path + "/"
            matches = [entry for candidate, entry in tree.items() if candidate.startswith(prefix)]
        else:
            matches = [tree[path]] if path in tree else []
        if not matches:
            raise ToolError("manifest_unmatched", path=path + ("/**" if subtree else ""))
        for entry in matches:
            if not _regular_entry(entry):
                raise ToolError("manifest_object_invalid", path=entry.path)
            if hygiene_reason(entry.path):
                raise ToolError("hygiene_prohibited", path=entry.path, detail="state=commit;reason=manifest_member")
            selected[entry.path] = entry
    if not selected:
        raise ToolError("manifest_empty")
    return {path: (entry.mode, read_blob(root, entry.sha)) for path, entry in sorted(selected.items())}


def validate_snapshot(root: Path, commit: str) -> tuple[dict, dict[str, tuple[str, bytes]]]:
    tree = list_tree(root, commit)
    check_tree_hygiene(tree)
    metadata = check_commit_source_state(root, tree)
    payload = resolve_payload(root, tree)
    return metadata, payload


def validate_command(root: Path, commit: str) -> tuple[dict, dict[str, tuple[str, bytes]]]:
    require_head(root, commit)
    require_clean_tracked(root)
    return validate_snapshot(root, commit)


def _zip_info(name: str, mode: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_FIXED_DATE)
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.reserved = 0
    info.flag_bits = _UTF8_FLAG
    info.volume = 0
    info.internal_attr = 0
    info.external_attr = int(mode, 8) << 16
    info.compress_type = zipfile.ZIP_STORED
    info.extra = b""
    info.comment = b""
    return info


def _write_canonical_zip(path: Path, prefix: str, payload: dict[str, tuple[str, bytes]]) -> None:
    local_struct = struct.Struct("<I5H3I2H")
    central_struct = struct.Struct("<I6H3I5H2I")
    end_struct = struct.Struct("<I4H2IH")
    records: list[tuple[bytes, int, int, int, int, int]] = []
    offset = 0
    with path.open("wb") as handle:
        for rel in sorted(payload):
            mode, data = payload[rel]
            name = (prefix + rel).encode("utf-8")
            if len(name) > 0xFFFF or len(data) > 0xFFFFFFFF or offset > 0xFFFFFFFF:
                raise ToolError("archive_too_large", path=rel)
            crc = binascii.crc32(data) & 0xFFFFFFFF
            size = len(data)
            header = local_struct.pack(
                0x04034B50, 20, _UTF8_FLAG, zipfile.ZIP_STORED, 0, _FIXED_DOS_DATE,
                crc, size, size, len(name), 0,
            )
            handle.write(header); handle.write(name); handle.write(data)
            records.append((name, int(mode, 8), crc, size, offset, len(header)))
            offset += len(header) + len(name) + size
        central_offset = offset
        for name, mode_value, crc, size, local_offset, _ in records:
            header = central_struct.pack(
                0x02014B50, (3 << 8) | 20, 20, _UTF8_FLAG, zipfile.ZIP_STORED, 0, _FIXED_DOS_DATE,
                crc, size, size, len(name), 0, 0, 0, 0, mode_value << 16, local_offset,
            )
            handle.write(header); handle.write(name)
            offset += len(header) + len(name)
        central_size = offset - central_offset
        if len(records) > 0xFFFF or central_size > 0xFFFFFFFF or central_offset > 0xFFFFFFFF:
            raise ToolError("archive_too_large")
        handle.write(end_struct.pack(0x06054B50, 0, 0, len(records), len(records), central_size, central_offset, 0))


def _canonical_archive(root: Path, commit: str, destination: Path, metadata: dict, payload: dict[str, tuple[str, bytes]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"localMedBot-{metadata['version']}/"
    fd, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        _write_canonical_zip(temporary, prefix, payload)
        verify_archive(root, commit, temporary, metadata=metadata, payload=payload)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_member_name(name: str, prefix: str) -> bool:
    if "\\" in name or name.startswith("/") or not name.startswith(prefix):
        return False
    tail = name[len(prefix):]
    if not tail or tail.endswith("/"):
        return False
    parts = PurePosixPath(tail).parts
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _check_local_header(raw: bytes, info: zipfile.ZipInfo, expected_name: str) -> None:
    offset = info.header_offset
    if offset < 0 or offset + _LOCAL_HEADER.size > len(raw):
        raise ToolError("archive_metadata_invalid", path=expected_name)
    fields = _LOCAL_HEADER.unpack_from(raw, offset)
    signature, extract_version, flags, method, mod_time, mod_date, crc, comp_size, file_size, name_len, extra_len = fields
    if signature != _LOCAL_SIGNATURE:
        raise ToolError("archive_metadata_invalid", path=expected_name)
    if extract_version != 20 or flags != _UTF8_FLAG or method != zipfile.ZIP_STORED or mod_time != 0 or mod_date != _FIXED_DOS_DATE:
        raise ToolError("archive_metadata_invalid", path=expected_name)
    start = offset + _LOCAL_HEADER.size
    end = start + name_len
    if end + extra_len > len(raw):
        raise ToolError("archive_metadata_invalid", path=expected_name)
    if raw[start:end] != expected_name.encode("utf-8") or extra_len != 0:
        raise ToolError("archive_metadata_invalid", path=expected_name)
    if crc != info.CRC or comp_size != info.compress_size or file_size != info.file_size:
        raise ToolError("archive_metadata_invalid", path=expected_name)


def verify_archive(
    root: Path,
    commit: str,
    archive_path: Path,
    *,
    metadata: dict | None = None,
    payload: dict[str, tuple[str, bytes]] | None = None,
) -> tuple[dict, dict[str, tuple[str, bytes]]]:
    if metadata is None or payload is None:
        metadata, payload = validate_snapshot(root, commit)
    prefix = f"localMedBot-{metadata['version']}/"
    expected = {prefix + path: (path, mode, data) for path, (mode, data) in payload.items()}
    try:
        raw = archive_path.read_bytes()
    except OSError as exc:
        raise ToolError("archive_unreadable", path=str(archive_path)) from exc
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            if archive.comment != b"":
                raise ToolError("archive_metadata_invalid", detail="archive_comment")
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ToolError("archive_duplicate_member")
            for name in names:
                if not _safe_member_name(name, prefix):
                    raise ToolError("archive_unsafe_member", path=name)
            if set(names) != set(expected):
                missing = sorted(set(expected) - set(names))
                extra = sorted(set(names) - set(expected))
                detail = "missing=" + ",".join(missing[:3]) + ";extra=" + ",".join(extra[:3])
                raise ToolError("archive_members_mismatch", detail=detail)
            bad = archive.testzip()
            if bad is not None:
                raise ToolError("archive_corrupt", path=bad)
            for info in infos:
                rel, mode, data = expected[info.filename]
                if info.is_dir():
                    raise ToolError("archive_metadata_invalid", path=info.filename)
                if (
                    info.create_system != 3
                    or info.create_version != 20
                    or info.extract_version != 20
                    or info.reserved != 0
                    or info.flag_bits != _UTF8_FLAG
                    or info.volume != 0
                    or info.internal_attr != 0
                    or info.external_attr != (int(mode, 8) << 16)
                    or info.compress_type != zipfile.ZIP_STORED
                    or info.date_time != _FIXED_DATE
                    or info.extra != b""
                    or info.comment != b""
                    or info.compress_size != info.file_size
                ):
                    raise ToolError("archive_metadata_invalid", path=info.filename)
                actual = archive.read(info)
                if actual != data:
                    raise ToolError("archive_content_mismatch", path=rel)
                if info.CRC != (binascii.crc32(data) & 0xFFFFFFFF):
                    raise ToolError("archive_corrupt", path=rel)
                _check_local_header(raw, info, info.filename)
    except ToolError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, struct.error) as exc:
        raise ToolError("archive_corrupt", path=str(archive_path)) from exc
    return metadata, payload


def _local_tag_commit(root: Path, tag: str) -> str | None:
    exists = _run_git(root, ["show-ref", "--verify", "--quiet", f"refs/tags/{tag}"], check=False)
    if exists.returncode == 1:
        return None
    if exists.returncode != 0:
        raise ToolError("tag_state_uncertain", ref=tag)
    proc = _run_git(root, ["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], check=False)
    if proc.returncode != 0:
        raise ToolError("tag_state_uncertain", ref=tag)
    return proc.stdout.decode("ascii").strip().lower()


def _remote_tag_commit(root: Path, tag: str) -> str | None:
    remote = _run_git(root, ["remote", "get-url", "origin"], check=False)
    if remote.returncode != 0 or not remote.stdout.strip():
        raise ToolError("remote_unavailable", ref="origin")
    query = _run_git(root, ["ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], check=False)
    if query.returncode != 0:
        raise ToolError("remote_unavailable", ref="origin")
    if not query.stdout.strip():
        return None
    temporary = f"refs/localmedbot-release-check/{uuid.uuid4().hex}"
    try:
        fetched = _run_git(root, ["fetch", "--no-tags", "origin", f"refs/tags/{tag}:{temporary}"], check=False)
        if fetched.returncode != 0:
            raise ToolError("remote_unavailable", ref=tag)
        proc = _run_git(root, ["rev-parse", "--verify", f"{temporary}^{{commit}}"], check=False)
        if proc.returncode != 0:
            raise ToolError("tag_state_uncertain", ref=tag)
        return proc.stdout.decode("ascii").strip().lower()
    finally:
        _run_git(root, ["update-ref", "-d", temporary], check=False)


def _released_payload(root: Path, commit: str) -> dict[str, tuple[str, bytes]]:
    try:
        metadata, payload = validate_snapshot(root, commit)
        if not metadata.get("version"):
            raise ToolError("version_metadata_invalid")
        return payload
    except ToolError as exc:
        raise ToolError("version_already_released") from exc


def release_check(root: Path, commit: str) -> tuple[dict, dict[str, tuple[str, bytes]]]:
    metadata, payload = validate_command(root, commit)
    if metadata["releaseable"] is not True:
        raise ToolError("release_not_permitted")
    tag = "v" + metadata["version"]
    local = _local_tag_commit(root, tag)
    remote = _remote_tag_commit(root, tag)
    if local is not None and remote is not None and local != remote:
        raise ToolError("tag_conflict", ref=tag)
    previous = remote or local
    if previous is not None:
        old_payload = _released_payload(root, previous)
        if set(old_payload) != set(payload) or any(old_payload[path] != payload[path] for path in payload):
            raise ToolError("released_payload_changed", ref=tag)
        raise ToolError("version_already_released", ref=tag)
    return metadata, payload


def _sanitised_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _child_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _child_cli(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "localmedbot.exe"
    return venv / "bin" / "localmedbot"


def _run_stage(command: list[str], *, cwd: Path, env: dict[str, str], stage: str) -> bytes:
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise ToolError("smoke_failed", stage=stage) from exc
    if proc.returncode != 0:
        raise ToolError("smoke_failed", stage=stage)
    return proc.stdout


def _json_stage(command: list[str], *, cwd: Path, env: dict[str, str], stage: str) -> dict:
    output = _run_stage(command, cwd=cwd, env=env, stage=stage)
    try:
        value = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("smoke_failed", stage=stage) from exc
    if not isinstance(value, dict):
        raise ToolError("smoke_failed", stage=stage)
    return value


def smoke_archive(root: Path, commit: str, archive_path: Path) -> tuple[dict, dict[str, tuple[str, bytes]]]:
    metadata, payload = verify_archive(root, commit, archive_path)
    prefix = f"localMedBot-{metadata['version']}/"
    with tempfile.TemporaryDirectory(prefix="localmedbot-smoke-") as temporary_dir:
        base = Path(temporary_dir)
        extracted = base / prefix.rstrip("/")
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                by_name = {info.filename: info for info in archive.infolist()}
                for path in sorted(payload):
                    member = prefix + path
                    info = by_name[member]
                    target = (base / PurePosixPath(member)).resolve()
                    if not target.is_relative_to(base.resolve()):
                        raise ToolError("archive_unsafe_member", path=member)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
                    target.chmod(int(payload[path][0][-3:], 8))
        except ToolError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            raise ToolError("smoke_failed", stage="extract") from exc

        child_env = _sanitised_env()
        child_venv = extracted / ".env"
        _run_stage([sys.executable, "-m", "venv", str(child_venv)], cwd=extracted, env=child_env, stage="venv")
        child_python = _child_python(child_venv)
        _run_stage([str(child_python), "-m", "pip", "install", "."], cwd=extracted, env=child_env, stage="install")

        probe = (
            "import importlib.metadata,json,localmedbot,pathlib,sysconfig;"
            "p=pathlib.Path(localmedbot.__file__).resolve();"
            "site=pathlib.Path(sysconfig.get_paths()['purelib']).resolve();"
            "print(json.dumps({'origin':str(p),'site':str(site),'runtime':localmedbot.__version__,"
            "'installed':importlib.metadata.version('localMedBot')}))"
        )
        probe_result = _json_stage([str(child_python), "-c", probe], cwd=extracted, env=child_env, stage="import_origin")
        try:
            origin = Path(probe_result["origin"]).resolve()
            site = Path(probe_result["site"]).resolve()
        except (KeyError, TypeError, OSError) as exc:
            raise ToolError("smoke_failed", stage="import_origin") from exc
        if not origin.is_relative_to(site) or probe_result.get("runtime") != metadata["version"] or probe_result.get("installed") != metadata["version"]:
            raise ToolError("smoke_failed", stage="import_origin")

        cli = _child_cli(child_venv)
        checked = _json_stage([str(cli), "check"], cwd=extracted, env=child_env, stage="check")
        expected_apps = sum(1 for path in payload if path.startswith("applications/") and path.endswith("/application.yaml") and len(path.split("/")) == 3)
        if checked.get("status") != "valid" or checked.get("version") != metadata["version"] or checked.get("applications") != expected_apps or expected_apps < 2:
            raise ToolError("smoke_failed", stage="check")

        run = _json_stage(
            [str(cli), "--data", ".localmedbot-smoke", "run", "guideline_qa", "--profile", "guideline_qa.recorded.default", "--example", "standard"],
            cwd=extracted,
            env=child_env,
            stage="recorded_workflow",
        )
        if run.get("status") != "waiting_review" or not isinstance(run.get("id"), str):
            raise ToolError("smoke_failed", stage="recorded_workflow")
        status = _json_stage([str(cli), "--data", ".localmedbot-smoke", "status", run["id"]], cwd=extracted, env=child_env, stage="status")
        artifacts = status.get("artifacts")
        workflow_output = status.get("run", {}).get("snapshot", {}).get("workflow", {}).get("output")
        if not isinstance(artifacts, dict) or not workflow_output or workflow_output not in artifacts:
            raise ToolError("smoke_failed", stage="output_artifact")
    return metadata, payload


def _archive_output(root: Path, value: str | None, version: str, preview: bool) -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else (root / path)
    suffix = "-preview" if preview else ""
    return root / "dist" / f"localMedBot-{version}{suffix}.zip"


def main(argv=None) -> int:
    parser = QuietParser(prog="release.py", add_help=True)
    parser.add_argument("--ref", default="HEAD")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=QuietParser)
    sub.add_parser("validate")
    build = sub.add_parser("build")
    build.add_argument("--preview", action="store_true")
    build.add_argument("--output")
    verify = sub.add_parser("verify")
    verify.add_argument("--archive", required=True)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--archive", required=True)
    sub.add_parser("release-check")
    try:
        args = parser.parse_args(argv)
        root = checkout_root(__file__)
        ensure_environment(root)
        commit = resolve_commit(root, args.ref)
        require_head(root, commit)
        if args.command == "validate":
            metadata, _ = validate_command(root, commit)
            return json_success({"status": "ok", "ref": commit, "version": metadata["version"]})
        if args.command == "release-check":
            metadata, _ = release_check(root, commit)
            return json_success({"status": "ok", "ref": commit, "version": metadata["version"], "publication_permitted": True})
        if args.command == "build":
            if args.preview:
                metadata, payload = validate_command(root, commit)
                permitted = False
            else:
                metadata, payload = release_check(root, commit)
                permitted = True
            output = _archive_output(root, args.output, metadata["version"], args.preview)
            _canonical_archive(root, commit, output, metadata, payload)
            return json_success({"status": "ok", "ref": commit, "version": metadata["version"], "archive": str(output), "publication_permitted": permitted})
        archive = Path(args.archive)
        if not archive.is_absolute():
            archive = root / archive
        if args.command == "verify":
            metadata, _ = verify_archive(root, commit, archive)
        elif args.command == "smoke":
            metadata, _ = smoke_archive(root, commit, archive)
        else:
            raise ToolError("argument_invalid")
        return json_success({"status": "ok", "ref": commit, "version": metadata["version"], "archive": str(archive)})
    except ToolError as exc:
        return json_failure(exc)
    except Exception:
        return json_failure(ToolError("internal_error"))


if __name__ == "__main__":
    raise SystemExit(main())
