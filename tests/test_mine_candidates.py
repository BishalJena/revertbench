import subprocess
from pathlib import Path

import pytest

from revertbench.mine.candidates import discover_candidates, discover_report, production_paths
from revertbench.mine.git import GitRepository


def _commit(path: Path, message: str) -> str:
    for args in (("add", "."), ("commit", "--quiet", "-m", message)):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args], check=True
        )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path, *, fix_only: bool = False) -> tuple[GitRepository, str]:
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(source), "config", key, value], check=True)
    (source / "tests").mkdir()
    (source / "LICENSE").write_text("Example upstream license\n")
    (source / "pkg.py").write_text("def normalize(value):\n    return value.lower()\n")
    (source / "tests" / "test_api.py").write_text("import pkg\ndef test_old():\n    assert True\n")
    _commit(source, "Initial working behavior")
    (source / "pkg.py").write_text("def normalize(value):\n    return value\n")
    _commit(source, "Introduce casing regression")
    (source / "pkg.py").write_text(
        "def normalize(value):\n    return value.lower()\n"
        + ("def added_in_fix():\n    return True\n" if fix_only else "")
    )
    (source / "tests" / "test_api.py").write_text(
        "# Copyright upstream example\nimport pkg\n"
        "def test_old():\n    assert True\n"
        "def test_regression():\n"
        + (
            "    assert pkg.added_in_fix()\n"
            if fix_only
            else "    assert pkg.normalize('X') == 'x'\n"
        )
    )
    fix = _commit(source, "Fix casing regression")
    return GitRepository(source, repo="local/toy"), fix


def test_candidate_production_patch_and_reference_are_separate(tmp_path: Path) -> None:
    repository, fix = _repository(tmp_path)
    candidates = discover_candidates(repository, limit=5)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.fix_commit == fix
    assert candidate.fix_parent == repository.parent(fix)
    assert candidate.modified_source_paths == ("pkg.py",)
    assert "tests/test_api.py" not in candidate.production_patch
    assert candidate.reference_node_id == "tests/test_api.py::test_regression"
    assert "assert pkg.normalize('X') == 'x'" in candidate.reference_source
    assert "test_old" not in candidate.reference_source
    assert candidate.license_text == "Example upstream license\n"
    assert candidate.reference_header == "# Copyright upstream example"
    assert candidate.base_commit is None and candidate.bad_commit is None
    assert candidate.commit_url is None


def test_fix_only_reference_is_rejected_before_any_execution(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path, fix_only=True)
    report = discover_report(repository)

    assert report.candidates == ()
    assert any("policy" in reason for _, reason in report.rejections)


def test_mining_never_imports_target_code(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    marker = tmp_path / "executed"
    original = (repository.path / "pkg.py").read_text()
    (repository.path / "pkg.py").write_text(
        f"open({str(marker)!r},'w').write('executed')\n" + original
    )
    (repository.path / "tests" / "test_api.py").write_text(
        "import pkg\ndef test_regression():\n    assert pkg.normalize('X') == 'x'\n"
    )
    _commit(repository.path, "Fix another regression with a test")
    discover_candidates(repository)
    assert not marker.exists()


def test_unittest_candidate_retains_original_node_and_explicit_derivation(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    (repository.path / "pkg.py").write_text(
        "def normalize(value):\n    return value.strip().lower()\n"
    )
    (repository.path / "tests" / "test_api.py").write_text(
        "# Copyright upstream example\nfrom unittest import TestCase\nimport pkg\n"
        "class ProductTests(TestCase):\n"
        "    def test_whitespace(self):\n"
        "        self.assertEqual(pkg.normalize(' X '), 'x')\n"
    )
    fix = _commit(repository.path, "Fix whitespace regression")
    candidates = discover_candidates(repository, limit=5)
    selected = next(candidate for candidate in candidates if candidate.fix_commit == fix)
    assert selected.reference_node_id == "tests/test_api.py::ProductTests::test_whitespace"
    assert selected.reference_derivation == "unittest_method_dependency_closure"
    assert "self.assertEqual(pkg.normalize(' X '), 'x')" in selected.reference_source
    assert "self = _revertbench_test_case()" in selected.reference_source
    assert selected.reference_header == "# Copyright upstream example"
    assert selected.license_text == "Example upstream license\n"


def test_plain_pytest_candidate_uses_source_selected_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from revertbench.grade.policy import PolicyResult
    from revertbench.mine import candidates as mining

    repository, _ = _repository(tmp_path)
    (repository.path / "pkg.py").write_text(
        "def normalize(value):\n    return value.strip().lower()\n"
    )
    (repository.path / "tests/test_api.py").write_text(
        "import pkg\nclass TestNormalize:\n"
        "    def test_whitespace(self):\n        assert pkg.normalize(' X ') == 'x'\n"
    )
    fix = _commit(repository.path, "Fix whitespace regression")
    # Isolate provenance from the independently owned policy capability.
    monkeypatch.setattr(mining, "check_submission", lambda *args: PolicyResult(True, None))
    selected = next(c for c in discover_candidates(repository, limit=5) if c.fix_commit == fix)

    assert selected.reference_derivation == "plain_pytest_method_dependency_closure_v1"
    assert selected.reference_node_id == "tests/test_api.py::TestNormalize::test_whitespace"
    assert "class TestNormalize:" in selected.reference_source
    assert "def test_whitespace(self):" in selected.reference_source


@pytest.mark.parametrize("unittest_method", [False, True])
def test_root_test_py_reference_is_discovered_without_entering_production_patch(
    tmp_path: Path, unittest_method: bool
) -> None:
    repository, _ = _repository(tmp_path)
    (repository.path / "pkg.py").write_text(
        "def normalize(value):\n    return value.strip().lower()\n"
    )
    reference = (
        "from unittest import TestCase\nimport pkg\n"
        "class NormalizeTests(TestCase):\n"
        "    def test_whitespace(self):\n"
        "        self.assertEqual(pkg.normalize(' X '), 'x')\n"
        if unittest_method
        else "import pkg\ndef test_whitespace():\n    assert pkg.normalize(' X ') == 'x'\n"
    )
    (repository.path / "test.py").write_text(reference)
    fix = _commit(repository.path, "Fix whitespace regression with root test.py")

    selected = [
        candidate
        for candidate in discover_candidates(repository, limit=5)
        if candidate.fix_commit == fix
    ]

    assert len(selected) == 1
    candidate = selected[0]
    assert candidate.modified_source_paths == ("pkg.py",)
    assert "test.py" not in candidate.production_patch
    node = "NormalizeTests::test_whitespace" if unittest_method else "test_whitespace"
    assert candidate.reference_node_id == f"test.py::{node}"
    observation = (
        "self.assertEqual(pkg.normalize(' X '), 'x')"
        if unittest_method
        else "assert pkg.normalize(' X ') == 'x'"
    )
    assert observation in candidate.reference_source


def test_root_test_py_classification_preserves_other_production_filenames() -> None:
    assert production_paths(
        ("test.py", "pkg/test.py", "foo_test.py", "test_api.py", "tests/foo.py", "pkg.py")
    ) == ("pkg/test.py", "foo_test.py", "pkg.py")
