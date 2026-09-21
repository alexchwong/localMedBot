"""Product version metadata parsing and loading."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_REQUIRED_KEYS = {"version", "releaseable"}


class VersionMetadataError(ValueError):
    """Raised when product lifecycle metadata is absent or malformed."""


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VersionMetadataError(f"duplicate_key:{key}")
        result[key] = value
    return result


def parse_version_metadata(data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise VersionMetadataError("metadata_bytes_required")
    try:
        text = bytes(data).decode("utf-8")
        value = json.loads(text, object_pairs_hook=_pairs_object)
    except VersionMetadataError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionMetadataError("malformed_json") from exc
    if not isinstance(value, dict):
        raise VersionMetadataError("metadata_object_required")
    if set(value) != _REQUIRED_KEYS:
        missing = sorted(_REQUIRED_KEYS - set(value))
        unknown = sorted(set(value) - _REQUIRED_KEYS)
        if missing:
            raise VersionMetadataError("missing_keys:" + ",".join(missing))
        raise VersionMetadataError("unknown_keys:" + ",".join(unknown))
    version = value["version"]
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        raise VersionMetadataError("invalid_version")
    if type(value["releaseable"]) is not bool:
        raise VersionMetadataError("invalid_releaseable")
    return {"version": version, "releaseable": value["releaseable"]}


def load_version_metadata(path: Path | None = None) -> dict[str, Any]:
    target = path or (Path(__file__).with_name("version.json"))
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise VersionMetadataError("metadata_unreadable") from exc
    return parse_version_metadata(data)
