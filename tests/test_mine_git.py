import subprocess
from pathlib import Path

import pytest

from revertbench.mine.git import GitError, GitRepository, find_regression_boundary


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> tuple[GitRepository, list[str]]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    commits = []
    for number in range(4):
        (source / "pkg.py").write_text(f"VALUE = {number}\n")
        _git(source, "add", "pkg.py")
        _git(source, "commit", "--quiet", "-m", f"change {number}")
        commits.append(_git(source, "rev-parse", "HEAD"))
    return GitRepository(source, repo="local/toy", work_root=tmp_path / "worktrees"), commits


def test_exact_history_and_source_reads(history: tuple[GitRepository, list[str]]) -> None:
    repository, commits = history
    assert repository.resolve_commit(commits[0]) == commits[0]
    assert repository.parent(commits[1]) == commits[0]
    assert repository.file_text(commits[0], "pkg.py") == "VALUE = 0\n"
    assert repository.commits_between(commits[0], commits[3]) == commits
    for revision in ("HEAD", "--all", "main~1"):
        with pytest.raises(ValueError):
            repository.resolve_commit(revision)
    with pytest.raises(ValueError):
        repository.file_text(commits[0], "../outside")


def test_detached_worktree_cleanup_preserves_the_original_checkout(
    history: tuple[GitRepository, list[str]],
) -> None:
    repository, commits = history
    original = repository.path / "pkg.py"
    original.write_text("user uncommitted change\n")
    with repository.worktree(commits[0]) as tree:
        assert (tree / "pkg.py").read_text() == "VALUE = 0\n"
        assert _git(tree, "rev-parse", "HEAD") == commits[0]
        selected = tree
    assert not selected.exists()
    assert original.read_text() == "user uncommitted change\n"
    assert len(_git(repository.path, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_export_and_patch_only_touch_registered_fresh_snapshots(
    history: tuple[GitRepository, list[str]],
    tmp_path: Path,
) -> None:
    repository, commits = history
    exported = repository.export_tree(commits[0], tmp_path / "export")
    assert not (exported / ".git").exists()
    patch = repository.diff(commits[0], commits[1], paths=("pkg.py",))
    repository.apply_patch(exported, patch, check=True)
    assert (exported / "pkg.py").read_text() == "VALUE = 0\n"
    repository.apply_patch(exported, patch)
    assert (exported / "pkg.py").read_text() == "VALUE = 1\n"
    with pytest.raises(GitError):
        repository.apply_patch(repository.path, patch)
    with pytest.raises(GitError):
        repository.export_tree(commits[0], repository.path)


def test_export_rejects_tracked_symlinks_before_copying(
    history: tuple[GitRepository, list[str]],
    tmp_path: Path,
) -> None:
    repository, _ = history
    (repository.path / "link.py").symlink_to("/etc/passwd")
    _git(repository.path, "add", "link.py")
    _git(repository.path, "commit", "--quiet", "-m", "unsafe link")
    sha = _git(repository.path, "rev-parse", "HEAD")
    with pytest.raises(GitError, match="link"):
        repository.export_tree(sha, tmp_path / "export")


def test_history_probe_establishes_a_unique_pass_to_fail_transition(
    history: tuple[GitRepository, list[str]],
) -> None:
    repository, commits = history
    result = find_regression_boundary(
        repository,
        commits[0],
        commits[-1],
        lambda sha: "pass" if sha in commits[:2] else "fail",
    )
    assert result.base_commit == commits[1]
    assert result.bad_commit == commits[2]
    assert len(result.observations) == 4


@pytest.mark.parametrize(
    "outcomes",
    [
        ("pass", "inconclusive", "fail", "fail"),
        ("pass", "fail", "pass", "fail"),
        ("pass", "pass", "pass", "pass"),
    ],
)
def test_inconclusive_nonmonotonic_or_missing_failure_is_rejected(
    history: tuple[GitRepository, list[str]],
    outcomes: tuple[str, ...],
) -> None:
    repository, commits = history
    states = dict(zip(commits, outcomes, strict=True))
    with pytest.raises(GitError):
        find_regression_boundary(repository, commits[0], commits[-1], lambda sha: states[sha])


def test_history_budget_is_checked_before_any_probe(
    history: tuple[GitRepository, list[str]],
) -> None:
    repository, commits = history
    called: list[str] = []

    def probe(sha: str) -> str:
        called.append(sha)
        return "fail"

    with pytest.raises(GitError, match="budget"):
        find_regression_boundary(
            repository,
            commits[0],
            commits[-1],
            probe,
            max_commits=2,
        )
    assert not called


def test_patch_of_nested_snapshot_does_not_discover_parent_git(
    history: tuple[GitRepository, list[str]],
) -> None:
    repository, commits = history
    snapshot = repository.export_tree(commits[0], repository.path / "nested-snapshot")
    head = _git(repository.path, "rev-parse", "HEAD")
    index = _git(repository.path, "diff", "--cached")
    repository.apply_patch(snapshot, repository.diff(commits[0], commits[1], paths=("pkg.py",)))
    assert (snapshot / "pkg.py").read_text() == "VALUE = 1\n"
    assert (repository.path / "pkg.py").read_text() == "VALUE = 3\n"
    assert _git(repository.path, "rev-parse", "HEAD") == head
    assert _git(repository.path, "diff", "--cached") == index


def test_worktree_ignores_repository_hooks_and_smudge_commands(
    history: tuple[GitRepository, list[str]],
    tmp_path: Path,
) -> None:
    import shlex

    repository, _ = history
    (repository.path / ".gitattributes").write_text("*.py filter=controlled\n")
    _git(repository.path, "add", ".gitattributes")
    _git(repository.path, "commit", "--quiet", "-m", "Add filter declaration")
    marker = tmp_path / "smudge-executed"
    _git(
        repository.path,
        "config",
        "filter.controlled.smudge",
        f"touch {shlex.quote(str(marker))}; cat",
    )
    _git(repository.path, "config", "filter.controlled.required", "true")
    hook_marker = tmp_path / "hook-executed"
    hook = repository.path / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(hook_marker))}\n")
    hook.chmod(0o755)
    reloaded = GitRepository(
        repository.path, repo="local/toy", work_root=tmp_path / "safe-worktrees"
    )
    with reloaded.worktree(reloaded.head()):
        pass
    assert not marker.exists()
    assert not hook_marker.exists()


def test_ancestry_uses_exact_commits(history: tuple[GitRepository, list[str]]) -> None:
    repository, commits = history
    assert repository.is_ancestor(commits[0], commits[-1])
    assert not repository.is_ancestor(commits[-1], commits[0])


def test_snapshot_preflight_rejects_size_before_checkout(
    history: tuple[GitRepository, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import revertbench.mine.git as mining_git

    repository, commits = history
    monkeypatch.setattr(mining_git, "_MAX_SNAPSHOT_BYTES", 1, raising=False)
    with pytest.raises(GitError, match="byte_limit"):
        repository.export_tree(commits[0], tmp_path / "export")
    assert not repository.work_root.exists()
    assert not (tmp_path / "export").exists()


def test_snapshot_preflight_counts_directories(
    history: tuple[GitRepository, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import revertbench.mine.git as mining_git

    repository, _ = history
    (repository.path / "a" / "b").mkdir(parents=True)
    (repository.path / "a" / "b" / "nested.py").write_text("pass\n")
    _git(repository.path, "add", "a")
    _git(repository.path, "commit", "--quiet", "-m", "nested")
    monkeypatch.setattr(mining_git, "_MAX_SNAPSHOT_ENTRIES", 3, raising=False)
    with pytest.raises(GitError, match="entry_limit"):
        repository.export_tree(repository.head(), tmp_path / "export")
    assert not repository.work_root.exists()


def test_pinned_objects_ignore_local_replace_refs(history: tuple[GitRepository, list[str]]) -> None:
    repository, commits = history
    _git(repository.path, "replace", commits[0], commits[-1])
    assert repository.file_text(commits[0], "pkg.py") == "VALUE = 0\n"
    assert repository.parent(commits[1]) == commits[0]


def test_file_exists_distinguishes_absent_paths(history: tuple[GitRepository, list[str]]) -> None:
    repository, commits = history
    assert repository.file_exists(commits[0], "pkg.py")
    assert not repository.file_exists(commits[0], "tests/new_test.py")


def test_local_history_grafts_are_rejected(history: tuple[GitRepository, list[str]]) -> None:
    repository, commits = history
    grafts = repository.path / ".git" / "info" / "grafts"
    grafts.write_text(commits[-1] + "\n")
    with pytest.raises(GitError, match="grafts"):
        GitRepository(repository.path)


def test_snapshot_preflight_rejects_large_individual_blob(
    history: tuple[GitRepository, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import revertbench.mine.git as mining_git

    repository, commits = history
    monkeypatch.setattr(mining_git, "_MAX_SOURCE_FILE", 1)
    with pytest.raises(GitError, match="file_byte_limit"):
        repository.export_tree(commits[0], tmp_path / "export")
    assert not repository.work_root.exists()


def test_clone_resolves_relative_cache_before_selecting_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import revertbench.mine.git as mining_git

    monkeypatch.chdir(tmp_path)
    destinations: list[Path] = []

    def capture(cwd: Path, *arguments: str) -> bytes:
        destinations.append(Path(arguments[-1]))
        raise GitError("stop_before_network")

    monkeypatch.setattr(mining_git, "_run", capture)
    with pytest.raises(GitError, match="stop_before_network"):
        GitRepository.clone("owner/repo", Path("cache"))
    assert destinations == [tmp_path / "cache" / "owner__repo"]


def test_shallow_history_cannot_claim_a_complete_regression_boundary(
    history: tuple[GitRepository, list[str]],
) -> None:
    repository, commits = history
    (repository.path / ".git" / "shallow").write_text(commits[-1] + "\n")
    with pytest.raises(GitError, match="shallow"):
        GitRepository(repository.path)
