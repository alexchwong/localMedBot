"""Policy enforcement for fixture declarations and verbatim assertions.

`docs/devel-sysprompt.md` (Testing rule) forbids asserting verbatim material that repository
code produces or that is read from a non-test repository asset, and requires a test whose
assertions depend on a version-controlled fixture to declare that fixture. This module enforces
the mechanically decidable parts:

- every declared fixture reference names an existing tracked asset below `tests/fixtures/`;
- a declared fixture's identity is used by the test that declares it;
- test functions that reference repository fixture files by path carry a declaration;
- no test restates the product version label as a constant.

The path scan is deliberately best-effort. Fixtures loaded through the runtime by immutable id
and version leave no path literal behind, so the policy, not the scanner, carries that
obligation. This module excludes itself from the path scan because it names fixture paths while
checking them.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
import support

FIXTURE_ROOT = (TESTS / "fixtures").resolve()
POLICY_MODULE = Path(__file__).name
PRODUCT_VERSION_LITERAL = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
DRAFT_FIXTURE = "tests/fixtures/steps/clinical_letter/draft/fixture-clinical_letter.draft.standard.v1.json"
TAPE_FIXTURE = "tests/fixtures/steps/guideline_qa/reason/tape-guideline_qa.reason.standard.multicall.v1.json"

_DECLARATIONS = None
_FUNCTION_INDEX = {}
_CONSTANT_INDEX = {}


def _iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _load_module(path):
    spec = importlib.util.spec_from_file_location(f"policy_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _declarations():
    """Return (module path, test case, declared references) for every test in the suite."""
    global _DECLARATIONS
    if _DECLARATIONS is None:
        loader = unittest.TestLoader()
        rows = []
        for path in sorted(TESTS.glob("test_*.py")):
            for case in _iter_cases(loader.loadTestsFromModule(_load_module(path))):
                method = getattr(type(case), case._testMethodName, None)
                rows.append((path, case, support.fixture_dependencies_of(method)))
        _DECLARATIONS = rows
    return _DECLARATIONS


def _test_functions(path):
    """Index the test functions of a module by class name and function name."""
    if path not in _FUNCTION_INDEX:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found[(None, node.name)] = node
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                        found[(node.name, child.name)] = child
        _FUNCTION_INDEX[path] = found
    return _FUNCTION_INDEX[path]


def _function_node(path, case):
    """Return the AST node of a discovered test case, class-based or module-level."""
    found = _test_functions(path)
    return found.get((type(case).__name__, case._testMethodName)) or found.get((None, case._testMethodName))


def _declared_index():
    """Index declared references by module, class name and function name."""
    index = {}
    for path, case, refs in _declarations():
        class_name = None if type(case).__name__ == "FunctionTestCase" else type(case).__name__
        index[(path.name, class_name, case._testMethodName)] = refs
    return index


def _module_constants(path):
    """Index module-level string constants so a named path constant is recognised."""
    if path not in _CONSTANT_INDEX:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = node.value.value
        _CONSTANT_INDEX[path] = found
    return _CONSTANT_INDEX[path]


def _walk(target):
    for node in target if isinstance(target, (list, tuple)) else [target]:
        yield from ast.walk(node)


def _string_literals(target):
    return [child.value for child in _walk(target) if isinstance(child, ast.Constant) and isinstance(child.value, str)]


def _reference_names(target):
    names = {child.id for child in _walk(target) if isinstance(child, ast.Name)}
    return names | {child.attr for child in _walk(target) if isinstance(child, ast.Attribute)}


def _repository_fixture_file(value):
    """Return whether a string literal names an existing repository fixture file."""
    if not value or value.startswith("/") or "\\" in value:
        return False
    candidate = ROOT / value
    if not candidate.is_file():
        return False
    try:
        candidate.resolve().relative_to(FIXTURE_ROOT)
    except ValueError:
        return False
    return True


def _asset_identities(ref):
    path = ROOT / ref
    document = json.loads(path.read_text(encoding="utf-8"))
    if path.name.startswith("fixture-"):
        return {document["id"]}
    if path.name.startswith("tape-"):
        return {document["id"], document["fixture_ref"]["id"]}
    return {path.name, path.stem}


def _restates_product_version(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "__version__":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "__version__":
            return True
    return False


class FixtureTagHelperTests(unittest.TestCase):
    def test_reference_shape_validation(self):
        for bad in ("", "/absolute.json", "tests/fixtures/../secret.json", "applications/clinical_letter/application.yaml", "fixtures/steps/x.json"):
            with self.subTest(ref=bad), self.assertRaises(ValueError):
                support.normalize_refs([bad])
        with self.assertRaises(ValueError):
            support.normalize_refs([])
        self.assertEqual(support.normalize_refs([TAPE_FIXTURE, DRAFT_FIXTURE, DRAFT_FIXTURE]), (DRAFT_FIXTURE, TAPE_FIXTURE))

    @support.fixture_dependency(DRAFT_FIXTURE)
    def test_declared_fixture_is_recorded_and_visible(self):
        """A declared dependency is recorded and shown in the test description."""
        document = json.loads((ROOT / DRAFT_FIXTURE).read_text(encoding="utf-8"))
        self.assertEqual(document["id"], "clinical_letter.draft.standard")
        self.assertEqual(support.fixture_dependencies_of(self.test_declared_fixture_is_recorded_and_visible), (DRAFT_FIXTURE,))
        self.assertIn(f"[fixtures: {DRAFT_FIXTURE}]", self.shortDescription())


class DeclaredFixtureIntegrityTests(unittest.TestCase):
    def test_declared_paths_are_tracked_repository_fixtures(self):
        for path, case, refs in _declarations():
            for ref in refs:
                target = ROOT / ref
                with self.subTest(test=case.id(), ref=ref):
                    self.assertTrue(target.is_file(), "declared fixture is missing")
                    self.assertTrue(target.resolve().is_relative_to(FIXTURE_ROOT), "declared fixture is outside tests/fixtures")
                    if (ROOT / ".git").exists():
                        self.assertTrue(support.is_tracked(ref), "declared fixture is not tracked by Git")

    def test_declared_fixture_identity_is_used_by_its_test(self):
        for path, case, refs in _declarations():
            if not refs:
                continue
            node = _function_node(path, case)
            self.assertIsNotNone(node, f"{case.id()} is not indexed as a test function")
            literals = _string_literals(node.body)
            names = _reference_names(node.body)
            constants = _module_constants(path)
            for ref in refs:
                with self.subTest(test=case.id(), ref=ref):
                    identities = _asset_identities(ref)
                    used = any(identity in literal for literal in literals for identity in identities)
                    used = used or any(name in names and value == ref for name, value in constants.items())
                    self.assertTrue(used, "declared fixture identity is absent from the declaring test")


class RepositoryFixtureReferenceTests(unittest.TestCase):
    def test_repository_fixture_file_references_are_declared(self):
        declared = _declared_index()
        for path in sorted(TESTS.glob("test_*.py")):
            if path.name == POLICY_MODULE:
                continue
            for (class_name, function_name), node in _test_functions(path).items():
                hits = sorted({value for value in _string_literals(node.body) if _repository_fixture_file(value)})
                if hits:
                    with self.subTest(test=f"{path.name}:{class_name}.{function_name}"):
                        self.assertTrue(declared.get((path.name, class_name, function_name)), f"repository fixture referenced without a declaration: {hits}")


class ProductVersionLabelTests(unittest.TestCase):
    def test_no_test_restates_the_product_version_label(self):
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not node.func.attr.startswith("assert") or not _restates_product_version(node):
                    continue
                if any(isinstance(arg, ast.Constant) and isinstance(arg.value, str) and PRODUCT_VERSION_LITERAL.match(arg.value) for arg in node.args):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
