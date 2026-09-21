"""Test-side fixture dependency tags.

`docs/devel-sysprompt.md` (Testing rule) requires a test whose assertions depend on a
version-controlled fixture to declare that fixture. `fixture_dependency` records the
declaration on the test function and makes it visible in runner output, which prints the first
line of a test description.

Declarations are shape-checked here. Existence, version-control tracking and use are enforced
by `tests/test_fixture_policy.py`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = "tests/fixtures"
_TAG_PREFIX = "[fixtures: "


def normalize_refs(refs):
    """Validate and canonicalise repository-relative fixture references."""
    normalized = []
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"fixture_reference_invalid:{ref!r}")
        if ref.startswith("/") or "\\" in ref or not all(part not in {"", ".", ".."} for part in ref.split("/")):
            raise ValueError(f"fixture_reference_invalid:{ref}")
        if ref != FIXTURE_ROOT and not ref.startswith(FIXTURE_ROOT + "/"):
            raise ValueError(f"fixture_reference_outside_tests_fixtures:{ref}")
        if ref not in normalized:
            normalized.append(ref)
    if not normalized:
        raise ValueError("fixture_reference_missing")
    return tuple(sorted(normalized))


def fixture_dependency(*refs):
    """Declare the repository fixtures a test's assertions depend on."""
    declared = normalize_refs(refs)

    def decorate(func):
        combined = normalize_refs(tuple(getattr(func, "__fixture_dependencies__", ())) + declared)
        func.__fixture_dependencies__ = combined
        description = (func.__doc__ or func.__name__).rstrip()
        if _TAG_PREFIX not in description:
            func.__doc__ = f"{description} {_TAG_PREFIX}{', '.join(combined)}]"
        return func

    return decorate


def fixture_dependencies_of(target):
    """Return the declared fixture references of a test function or bound method."""
    return tuple(getattr(getattr(target, "__func__", target), "__fixture_dependencies__", ()))


def is_tracked(ref):
    """Return whether `ref` is tracked by Git in this checkout."""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", "--", ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0
