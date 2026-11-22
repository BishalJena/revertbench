"""A repair may change only explicit production hunks in a fresh bad snapshot."""

from pathlib import Path

import pytest

from revertbench.build.patch import BuildError, parse_production_patch
from revertbench.mine.models import MiningCandidate

PATCH = (
    "diff --git a/toy.py b/toy.py\nindex 1111111..2222222 100644\n"
    "--- a/toy.py\n+++ b/toy.py\n@@ -1,2 +1,2 @@\n"
    " def normalize(value):\n-    return value.strip()\n+    return value.strip().lower()\n"
)


def test_patch_identifies_only_changed_production_lines() -> None:
    patch = parse_production_patch(PATCH)
    assert patch.paths == ("toy.py",)
    assert patch.added_lines == {"toy.py": (2,)}
    assert patch.removed_lines == {"toy.py": (2,)}
    assert patch.changed_count == 2


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/outside.py",
        ".git/hooks/run.py",
        "tests/test_toy.py",
        "test.py",
        "conftest.py",
        "setup.py",
        "pyproject.toml",
        "a\\b.py",
    ],
)
def test_patch_rejects_nonproduction_or_escaping_paths(path: str) -> None:
    with pytest.raises(BuildError):
        parse_production_patch(PATCH.replace("toy.py", path))


@pytest.mark.parametrize("path", ["pkg/test.py", "foo_test.py"])
def test_root_test_py_exclusion_does_not_reclassify_other_source_paths(path: str) -> None:
    assert parse_production_patch(PATCH.replace("toy.py", path)).paths == (path,)


@pytest.mark.parametrize(
    "alteration",
    [
        "new file mode 120000\n",
        "old mode 100644\nnew mode 100755\n",
        "rename from toy.py\nrename to other.py\n",
        "GIT binary patch\n",
    ],
)
def test_patch_cannot_hide_file_type_mode_or_rename_changes(alteration: str) -> None:
    source = PATCH.replace("index 1111111..2222222 100644\n", alteration)
    with pytest.raises(BuildError):
        parse_production_patch(source)


def test_truncated_or_conflicting_hunks_are_rejected() -> None:
    for source in (PATCH.rsplit("+", 1)[0], PATCH.replace("@@ -1,2 +1,2 @@", "@@ -1,1 +1,1 @@")):
        with pytest.raises(BuildError):
            parse_production_patch(source)


def test_new_production_file_is_not_a_test_or_configuration_change() -> None:
    source = (
        "diff --git a/helper.py b/helper.py\nnew file mode 100644\n"
        "index 0000000..1111111\n--- /dev/null\n+++ b/helper.py\n"
        "@@ -0,0 +1,2 @@\n+def helper():\n+    return 1\n"
    )
    assert parse_production_patch(source).added_lines == {"helper.py": (1, 2)}


class _Repository:
    def __init__(self, *, linked: bool = False, extra_change: bool = False) -> None:
        self.linked = linked
        self.extra_change = extra_change
        self.ancestry = True
        self.unchanged_reference = False
        self.sentinel: Path | None = None

    def resolve_commit(self, commit: str) -> str:
        return commit

    def parent(self, commit: str) -> str:
        return "1" * 40 if commit == "2" * 40 else "3" * 40

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.ancestry

    def file_exists(self, commit: str, path: str) -> bool:
        return True

    def file_text(self, commit: str, path: str) -> str:
        if commit == "3" * 40 and not self.unchanged_reference:
            return (
                "from toy import normalize\ndef test_regression():\n"
                "    assert normalize(' X ') == 'X'\n"
            )
        return (
            "from toy import normalize\n\ndef test_regression():\n"
            "    assert normalize(' X ') == 'x'\n"
        )

    def changed_paths(self, before: str, after: str) -> tuple[str, ...]:
        return ("toy.py",)

    def diff(self, before: str, after: str, paths: tuple[str, ...] = ()) -> str:
        return PATCH

    def export_tree(self, commit: str, destination: Path) -> Path:
        destination.mkdir()
        if self.linked:
            assert self.sentinel is not None
            (destination / "toy.py").symlink_to(self.sentinel)
        else:
            (destination / "toy.py").write_text("def normalize(value):\n    return value.strip()\n")
        return destination

    def apply_patch(self, tree: Path, patch: str, *, check: bool = False) -> None:
        if not check:
            (tree / "toy.py").write_text(
                "def normalize(value):\n    return value.strip().lower()\n"
            )
            if self.extra_change:
                (tree / "unexpected.txt").write_text("must not be part of repair")


def _candidate() -> "MiningCandidate":
    from revertbench.mine.models import MiningCandidate

    return MiningCandidate(
        repo="owner/toy",
        base_commit="1" * 40,
        bad_commit="2" * 40,
        fix_parent="3" * 40,
        fix_commit="4" * 40,
        reference_path="tests/test_toy.py",
        reference_node_id="tests/test_toy.py::test_regression",
        reference_source=(
            "from toy import normalize\n\ndef test_regression():\n"
            "    assert normalize(' X ') == 'x'\n"
        ),
        production_patch=PATCH,
        modified_source_paths=("toy.py",),
    )


def test_materialization_keeps_original_bad_and_records_minimal_repair(tmp_path: Path) -> None:
    from revertbench.build import materialize_candidate

    built = materialize_candidate(_Repository(), _candidate(), tmp_path / "build")
    assert "lower" not in (built.bad_tree / "toy.py").read_text()
    assert "lower" in (built.repaired_tree / "toy.py").read_text()
    assert built.source_hashes["bad"] != built.source_hashes["repaired"]
    assert len(built.patch_sha256) == 64
    assert built.changed_lines == {"toy.py": (2,)}
    assert not (built.bad_tree / "tests/test_revertbench_submission.py").exists()


@pytest.mark.parametrize("alteration", [None, "tag", "body"])
def test_plain_pytest_provenance_is_verified_from_historical_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alteration: str | None
) -> None:
    from dataclasses import replace

    from revertbench.build import materialize_candidate
    from revertbench.mine.reference import extract_reference

    repository = _Repository()
    source = (
        "from toy import normalize\nclass TestNormalize:\n"
        "    def test_regression(self):\n        assert normalize(' X ') == 'x'\n"
    )

    def file_text(commit: str, path: str) -> str:
        return source.replace("'x'", "'X'") if commit == "3" * 40 else source

    monkeypatch.setattr(repository, "file_text", file_text)
    candidate = replace(
        _candidate(),
        reference_node_id="tests/test_toy.py::TestNormalize::test_regression",
        reference_source=extract_reference(source, "TestNormalize::test_regression"),
        reference_derivation="plain_pytest_method_dependency_closure_v1",
    )
    if alteration == "tag":
        candidate = replace(candidate, reference_derivation="unittest_method_dependency_closure")
    elif alteration == "body":
        candidate = replace(
            candidate, reference_source=candidate.reference_source.replace("'x'", "'z'")
        )
    if alteration is not None:
        with pytest.raises(BuildError):
            materialize_candidate(repository, candidate, tmp_path / "build")
        assert not (tmp_path / "build").exists()
    else:
        built = materialize_candidate(repository, candidate, tmp_path / "build")
        assert built.reference_source == candidate.reference_source
        assert built.reference_node_id == candidate.reference_node_id


@pytest.mark.parametrize("changed_count", [649, 701])
def test_materialization_bounds_introducing_refactors_separately_from_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_count: int
) -> None:
    from revertbench.build import materialize_candidate

    repository = _Repository()

    def diff(before: str, after: str, paths: tuple[str, ...] = ()) -> str:
        if (before, after) != ("1" * 40, "2" * 40):
            return PATCH
        assert paths == ("toy.py",)
        header = PATCH.split("@@", maxsplit=1)[0]
        return (
            f"{header}@@ -1,{changed_count} +1,2 @@\n"
            " def normalize(value):\n"
            + "-    pass\n" * (changed_count - 2)
            + "-    return value\n+    return value.strip()\n"
        )

    monkeypatch.setattr(repository, "diff", diff)
    if changed_count > 700:
        with pytest.raises(BuildError, match="unsupported_patch_size"):
            materialize_candidate(repository, _candidate(), tmp_path / "build")
    else:
        built = materialize_candidate(repository, _candidate(), tmp_path / "build")
        assert built.production_patch == PATCH
        assert built.changed_lines == {"toy.py": (2,)}


def test_materialization_rejects_link_before_copying_outside_source(tmp_path: Path) -> None:
    from revertbench.build import materialize_candidate

    repository = _Repository(linked=True)
    repository.sentinel = tmp_path / "outside-sentinel"
    repository.sentinel.write_text("OUTSIDE_SENTINEL_MUST_NOT_BE_COPIED")
    with pytest.raises(BuildError, match="source_archive"):
        materialize_candidate(repository, _candidate(), tmp_path / "build")
    assert not (tmp_path / "build" / "repaired" / "toy.py").exists()
    assert repository.sentinel.read_text() == "OUTSIDE_SENTINEL_MUST_NOT_BE_COPIED"


def test_materialization_rejects_changes_outside_the_production_patch(tmp_path: Path) -> None:
    from revertbench.build import materialize_candidate

    with pytest.raises(BuildError, match="unexpected_repair_change"):
        materialize_candidate(_Repository(extra_change=True), _candidate(), tmp_path / "build")


@pytest.mark.parametrize(
    "alteration", ["reference", "unchanged_reference", "ancestry", "parent", "patch"]
)
def test_unverified_historical_claims_are_rejected_before_source_build(
    tmp_path: Path, alteration: str
) -> None:
    from dataclasses import replace

    from revertbench.build import materialize_candidate

    repository = _Repository()
    candidate = _candidate()
    if alteration == "reference":
        candidate = replace(
            candidate, reference_source=candidate.reference_source.replace("'x'", "'invented'")
        )
    elif alteration == "unchanged_reference":
        repository.unchanged_reference = True
    elif alteration == "ancestry":
        repository.ancestry = False
    elif alteration == "parent":
        candidate = replace(candidate, base_commit="9" * 40)
    else:
        candidate = replace(candidate, production_patch=PATCH.replace(".lower()", ".upper()"))
    with pytest.raises(BuildError):
        materialize_candidate(repository, candidate, tmp_path / "build")
    assert not (tmp_path / "build").exists()


def test_real_managed_git_exports_apply_repair_without_parent_repo_mutation(tmp_path: Path) -> None:
    import subprocess

    from revertbench.build import materialize_candidate
    from revertbench.mine.git import GitRepository
    from revertbench.mine.reference import extract_reference

    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (source / "tests").mkdir()
    before = 'from toy import normalize\n\ndef test_existing():\n    assert normalize("x") == "x"\n'
    (source / "tests/test_toy.py").write_text(before)
    (source / "toy.py").write_text("def normalize(value):\n    return value.strip().lower()\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "base")
    base = git("rev-parse", "HEAD")
    (source / "toy.py").write_text("def normalize(value):\n    return value.strip()\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "bad")
    bad = git("rev-parse", "HEAD")
    after = before + '\ndef test_regression():\n    assert normalize(" X ") == "x"\n'
    (source / "tests/test_toy.py").write_text(after)
    (source / "toy.py").write_text("def normalize(value):\n    return value.strip().lower()\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "fix")
    fix = git("rev-parse", "HEAD")
    repository = GitRepository(source, repo="local/toy")
    candidate = MiningCandidate(
        repo="local/toy",
        base_commit=base,
        bad_commit=bad,
        fix_parent=bad,
        fix_commit=fix,
        reference_path="tests/test_toy.py",
        reference_node_id="tests/test_toy.py::test_regression",
        reference_source=extract_reference(after, "test_regression"),
        production_patch=repository.diff(bad, fix, paths=("toy.py",)),
        modified_source_paths=("toy.py",),
    )
    head = git("rev-parse", "HEAD")
    built = materialize_candidate(repository, candidate, source / "nested-artifacts" / "case")
    assert "lower" in (built.repaired_tree / "toy.py").read_text()
    assert git("rev-parse", "HEAD") == head
    assert (source / "toy.py").read_text() == (built.repaired_tree / "toy.py").read_text()


def test_new_historical_reference_file_does_not_require_parent_blob(tmp_path: Path) -> None:
    from revertbench.build import materialize_candidate

    class NewReference(_Repository):
        def file_exists(self, commit: str, path: str) -> bool:
            return commit != "3" * 40

        def file_text(self, commit: str, path: str) -> str:
            if commit == "3" * 40:
                raise AssertionError("must not read missing parent blob")
            return super().file_text(commit, path)

    built = materialize_candidate(NewReference(), _candidate(), tmp_path / "build")
    assert built.reference_source == _candidate().reference_source


def test_existing_regular_executable_source_mode_does_not_change_repair_semantics() -> None:
    regular = parse_production_patch(PATCH.replace(" 100644\n", " 100755\n"))
    assert regular.paths == ("toy.py",)
    assert regular.added_lines == {"toy.py": (2,)}
    for mode in ("120000", "160000"):
        with pytest.raises(BuildError):
            parse_production_patch(PATCH.replace(" 100644\n", f" {mode}\n"))


def test_builder_rejects_unsupported_or_mislabeled_reference_derivation(tmp_path: Path) -> None:
    from dataclasses import replace

    from revertbench.build import materialize_candidate

    candidate = replace(_candidate(), reference_derivation="unverified_rewrite")
    with pytest.raises(BuildError, match="reference_derivation"):
        materialize_candidate(_Repository(), candidate, tmp_path / "build")


def test_builder_passes_qualified_unittest_reference_to_deterministic_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    import revertbench.build.candidate as module
    from revertbench.build import materialize_candidate

    candidate = replace(
        _candidate(),
        reference_node_id="tests/test_toy.py::NormalizeTests::test_regression",
        reference_derivation="unittest_method_dependency_closure",
    )
    seen: list[str] = []
    monkeypatch.setattr(
        module, "changed_test_functions", lambda before, after: ("NormalizeTests::test_regression",)
    )

    def extract(source: str, function: str) -> str:
        seen.append(function)
        return candidate.reference_source

    monkeypatch.setattr(module, "extract_reference", extract)

    class ClassRepository(_Repository):
        def file_text(self, commit: str, path: str) -> str:
            return (
                "from unittest import TestCase\nclass NormalizeTests(TestCase):\n"
                "    def test_regression(self):\n        self.assertEqual(1, 1)\n"
            )

    built = materialize_candidate(ClassRepository(), candidate, tmp_path / "build")
    assert built.reference_node_id == candidate.reference_node_id
    assert seen == ["NormalizeTests::test_regression"]


def test_builder_verifies_actual_lifted_method_source(tmp_path: Path) -> None:
    from dataclasses import replace

    from revertbench.build import materialize_candidate
    from revertbench.mine.reference import extract_reference

    class ClassRepository(_Repository):
        def file_text(self, commit: str, path: str) -> str:
            expected = "X" if commit == "3" * 40 else "x"
            return (
                "from unittest import TestCase\nfrom toy import normalize\n\n"
                "class NormalizeTests(TestCase):\n"
                "    def test_regression(self):\n"
                f'        self.assertEqual(normalize(" X "), "{expected}")\n'
            )

    repository = ClassRepository()
    source = extract_reference(
        repository.file_text("4" * 40, "tests/test_toy.py"), "NormalizeTests::test_regression"
    )
    candidate = replace(
        _candidate(),
        reference_node_id="tests/test_toy.py::NormalizeTests::test_regression",
        reference_source=source,
        reference_derivation="unittest_method_dependency_closure",
    )
    built = materialize_candidate(repository, candidate, tmp_path / "build")
    assert built.reference_source == source
    assert "self.assertEqual" in source
    forged = replace(candidate, reference_source=source.replace('"x"', '"forged"'))
    with pytest.raises(BuildError, match="historical_extraction"):
        materialize_candidate(repository, forged, tmp_path / "forged")
