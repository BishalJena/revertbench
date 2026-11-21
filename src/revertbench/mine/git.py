"""Bounded Git object/history operations and disposable, source-only snapshots."""

from __future__ import annotations

import contextlib
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from revertbench.mine.models import BisectResult, CommitOutcome, exact_commit, relative_path

_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_BLOB = 1_048_576
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_MAX_SNAPSHOT_ENTRIES = 4096
_MAX_SOURCE_FILE = 4 * 1024 * 1024


class GitError(RuntimeError):
    """A repository or observed history does not meet the mining contract."""


def _environment(cwd: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CEILING_DIRECTORIES": str(cwd.parent.resolve()),
    }


def _command(cwd: Path, arguments: Sequence[str], config: Sequence[str]) -> list[str]:
    return [
        "git",
        "--no-pager",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotePath=false",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        *(value for setting in config for value in ("-c", setting)),
        "-C",
        str(cwd),
        *arguments,
    ]


def _run(
    cwd: Path,
    *arguments: str,
    input_text: str | None = None,
    allowed_codes: tuple[int, ...] = (0,),
    timeout: float = 60,
    config: Sequence[str] = (),
) -> bytes:
    with tempfile.TemporaryFile() as stdin:
        if input_text is not None:
            payload = input_text.encode("utf-8")
            if len(payload) > 2 * 1024 * 1024:
                raise GitError("patch_byte_limit")
            stdin.write(payload)
        stdin.seek(0)
        try:
            with subprocess.Popen(
                _command(cwd, arguments, config),
                cwd=cwd,
                env=_environment(cwd),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            ) as process:
                output, deadline, seen = bytearray(), time.monotonic() + timeout, 0
                assert process.stdout is not None and process.stderr is not None
                try:
                    with selectors.DefaultSelector() as selector:
                        for stream in (process.stdout, process.stderr):
                            os.set_blocking(stream.fileno(), False)
                            selector.register(stream, selectors.EVENT_READ)
                        while selector.get_map():
                            if time.monotonic() >= deadline:
                                raise GitError("git_timeout")
                            for key, _ in selector.select(0.05):
                                chunk = os.read(key.fd, 16_384)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                seen += len(chunk)
                                if seen > _MAX_OUTPUT:
                                    raise GitError("git_output_limit")
                                if key.fileobj is process.stdout:
                                    output.extend(chunk)
                    code = process.wait(timeout=max(deadline - time.monotonic(), 0.01))
                    if code not in allowed_codes:
                        raise GitError(f"git_command_failed:{code}")
                    return bytes(output)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitError("git_process_unavailable") from error


class GitRepository:
    """An explicitly selected object database; no upstream Python is executed."""

    def __init__(
        self,
        path: Path,
        *,
        repo: str = "local/toy",
        work_root: Path | None = None,
    ) -> None:
        self.path = path.resolve()
        self.repo = repo
        self.work_root = (work_root or self.path.parent / ".revertbench-worktrees").resolve()
        self._exports: set[Path] = set()
        self._safe_config: tuple[str, ...] = ()
        if not self.path.is_dir():
            raise GitError("repository_unavailable")
        git_directory = Path(self._git("rev-parse", "--git-common-dir").decode().strip())
        if not git_directory.is_absolute():
            git_directory = self.path / git_directory
        if (git_directory / "info" / "grafts").exists():
            raise GitError("unsupported_history_grafts")
        if (git_directory / "shallow").exists():
            raise GitError("unsupported_shallow_history")
        configured = self._git(
            "config",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$",
            allowed_codes=(0, 1),
        )
        drivers: set[str] = set()
        for key in configured.decode("utf-8").split("\0"):
            if not key:
                continue
            match = re.fullmatch(r"filter\.([A-Za-z0-9_.-]+)\.(clean|smudge|process|required)", key)
            if match is None:
                raise GitError("unsupported_filter_configuration")
            drivers.add(match.group(1))
        self._safe_config = tuple(
            f"filter.{driver}.{operation}={value}"
            for driver in sorted(drivers)
            for operation, value in (
                ("smudge", ""),
                ("process", ""),
                ("clean", ""),
                ("required", "false"),
            )
        )

    def _git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        input_text: str | None = None,
        allowed_codes: tuple[int, ...] = (0,),
        timeout: float = 60,
    ) -> bytes:
        return _run(
            cwd or self.path,
            *arguments,
            input_text=input_text,
            allowed_codes=allowed_codes,
            timeout=timeout,
            config=self._safe_config,
        )

    @classmethod
    def clone(cls, repo: str, cache_dir: Path) -> GitRepository:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
            raise ValueError("Repository must be a public GitHub owner/name identifier")
        if any(part in {".", ".."} for part in repo.split("/")):
            raise ValueError("Repository identifier cannot contain traversal")
        cache_dir = cache_dir.resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)
        destination = cache_dir / repo.replace("/", "__")
        if not destination.exists():
            _run(
                cache_dir,
                "clone",
                "--mirror",
                "--",
                f"https://github.com/{repo}.git",
                str(destination),
            )
        repository = cls(destination, repo=repo)
        expected = f"https://github.com/{repo}.git"
        if repository.remote_url() not in {expected, expected.removesuffix(".git")}:
            raise GitError("cache_repository_identity_mismatch")
        return repository

    def remote_url(self) -> str | None:
        data = self._git("config", "--get", "remote.origin.url", allowed_codes=(0, 1))
        return data.decode("utf-8").strip() or None

    def resolve_commit(self, commit: str) -> str:
        exact_commit(commit)
        result = self._git("rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
        if result != commit:
            raise GitError("object_is_not_the_exact_commit")
        return exact_commit(result)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor, descendant = self.resolve_commit(ancestor), self.resolve_commit(descendant)
        common = (
            self._git("merge-base", ancestor, descendant, allowed_codes=(0, 1)).decode().strip()
        )
        return common == ancestor

    def head(self) -> str:
        return exact_commit(self._git("rev-parse", "HEAD").decode().strip())

    def parent(self, commit: str) -> str:
        fields = self._git("rev-list", "--parents", "-n", "1", self.resolve_commit(commit)).split()
        if len(fields) < 2:
            raise GitError("commit_has_no_parent")
        return exact_commit(fields[1].decode())

    def first_parent_history(self, tip: str | None = None, *, limit: int = 1000) -> list[str]:
        if not 1 <= limit <= 10_000:
            raise ValueError("History limit must be between 1 and 10000")
        selected = self.resolve_commit(tip) if tip is not None else self.head()
        return [
            exact_commit(line)
            for line in self._git(
                "rev-list",
                "--first-parent",
                f"--max-count={limit}",
                selected,
            )
            .decode()
            .splitlines()
        ]

    def commits_between(self, good: str, bad: str, *, limit: int = 128) -> list[str]:
        good, bad = self.resolve_commit(good), self.resolve_commit(bad)
        commits = (
            self._git(
                "rev-list",
                "--first-parent",
                "--reverse",
                f"--max-count={limit + 1}",
                f"{good}..{bad}",
            )
            .decode()
            .splitlines()
        )
        if len(commits) + 1 > limit:
            raise GitError("history_budget_exceeded")
        if not commits or self.parent(commits[0]) != good or commits[-1] != bad:
            raise GitError("history_is_not_a_linear_first_parent_interval")
        return [good, *(exact_commit(commit) for commit in commits)]

    def file_exists(self, commit: str, path: str) -> bool:
        data = self._git("ls-tree", "-z", self.resolve_commit(commit), "--", relative_path(path))
        return bool(data)

    def file_text(self, commit: str, path: str) -> str:
        selected = f"{self.resolve_commit(commit)}:{relative_path(path)}"
        size = int(self._git("cat-file", "-s", selected).strip())
        if size > _MAX_BLOB:
            raise GitError("source_blob_byte_limit")
        try:
            return self._git("cat-file", "blob", selected).decode("utf-8")
        except UnicodeError as error:
            raise GitError("source_blob_not_utf8") from error

    def changed_paths(self, before: str, after: str) -> tuple[str, ...]:
        data = self._git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            self.resolve_commit(before),
            self.resolve_commit(after),
            "--",
        )
        return tuple(relative_path(value.decode("utf-8")) for value in data.split(b"\0") if value)

    def diff(self, before: str, after: str, *, paths: Sequence[str] = ()) -> str:
        selected = [relative_path(path) for path in paths]
        return self._git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--unified=3",
            self.resolve_commit(before),
            self.resolve_commit(after),
            "--",
            *selected,
        ).decode("utf-8")

    def commit_metadata(self, commit: str) -> tuple[str, str, str]:
        data = self._git(
            "show",
            "--no-patch",
            "--format=%s%x00%cI%x00%B",
            self.resolve_commit(commit),
        ).decode("utf-8")
        title, committed_at, body = data.split("\0", 2)
        return title.strip(), committed_at.strip(), body.strip()

    def _preflight_snapshot(self, commit: str) -> None:
        records = self._git("ls-tree", "-r", "-l", "-z", commit)
        entries: set[str] = set()
        total = 0
        for record in records.split(b"\0"):
            if not record:
                continue
            header, raw_path = record.split(b"\t", 1)
            mode, kind, _, raw_size = header.split()
            try:
                name = relative_path(raw_path.decode("utf-8"))
            except (UnicodeError, ValueError) as error:
                raise GitError("unsupported_snapshot_path") from error
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise GitError("snapshot_contains_unsupported_link_or_mode")
            if any(part.lower() == ".git" for part in Path(name).parts):
                raise GitError("snapshot_contains_git_metadata")
            size = int(raw_size)
            if size > _MAX_SOURCE_FILE:
                raise GitError("snapshot_source_file_byte_limit")
            total += size
            if total > _MAX_SNAPSHOT_BYTES:
                raise GitError("snapshot_source_byte_limit")
            path = Path(name)
            entries.add(name)
            entries.update(parent.as_posix() for parent in path.parents if parent != Path("."))
            if len(entries) > _MAX_SNAPSHOT_ENTRIES:
                raise GitError("snapshot_source_entry_limit")

    @contextlib.contextmanager
    def worktree(self, commit: str) -> Iterator[Path]:
        selected = self.resolve_commit(commit)
        self._preflight_snapshot(selected)
        self.work_root.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="snapshot-", dir=self.work_root))
        path.rmdir()
        added = False
        try:
            self._git("worktree", "add", "--detach", "--", str(path), selected)
            added = True
            yield path
        finally:
            if added:
                self._git("worktree", "remove", "--force", "--", str(path))
            elif path.exists():
                shutil.rmtree(path)

    def export_tree(self, commit: str, destination: Path) -> Path:
        if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
            raise GitError("snapshot_destination_must_be_fresh")
        destination = destination.resolve()
        if destination == self.path or destination == self.work_root:
            raise GitError("snapshot_destination_is_not_owned")
        with self.worktree(commit) as source:
            destination.mkdir(parents=True, exist_ok=True)
            try:
                _copy_snapshot(source, destination)
            except (GitError, OSError):
                shutil.rmtree(destination)
                raise
        self._exports.add(destination)
        return destination

    def apply_patch(self, tree: Path, patch: str, *, check: bool = False) -> None:
        if tree.is_symlink() or tree.resolve() not in self._exports:
            raise GitError("patch_destination_is_not_an_owned_snapshot")
        if any(mode in patch for mode in ("120000", "160000")):
            raise GitError("patch_cannot_create_links_or_submodules")
        summary = self._git("apply", "--numstat", "-z", "--", cwd=tree, input_text=patch)
        for record in summary.split(b"\0"):
            if not record:
                continue
            name = relative_path(record.split(b"\t", 2)[-1].decode("utf-8"))
            if any(part.lower() == ".git" for part in Path(name).parts):
                raise GitError("patch_cannot_modify_git_metadata")
        self._git("apply", "--check", "--whitespace=nowarn", "--", cwd=tree, input_text=patch)
        if not check:
            self._git("apply", "--whitespace=nowarn", "--", cwd=tree, input_text=patch)


def _copy_snapshot(source: Path, destination: Path) -> None:
    entries, total = 0, 0
    for root, directories, files in os.walk(source, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        for name in [*directories, *files]:
            if name == ".git":
                continue
            path = Path(root) / name
            metadata = path.lstat()
            entries += 1
            if entries > _MAX_SNAPSHOT_ENTRIES:
                raise GitError("snapshot_source_entry_limit")
            if stat.S_ISLNK(metadata.st_mode) or (path.is_file() and metadata.st_nlink != 1):
                raise GitError("snapshot_contains_unsupported_link")
            target = destination / path.relative_to(source)
            if stat.S_ISDIR(metadata.st_mode):
                target.mkdir(exist_ok=True)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_size > _MAX_SOURCE_FILE:
                    raise GitError("snapshot_source_file_byte_limit")
                total += metadata.st_size
                if total > _MAX_SNAPSHOT_BYTES:
                    raise GitError("snapshot_source_byte_limit")
                with path.open("rb") as incoming, target.open("xb") as outgoing:
                    copied = 0
                    while chunk := incoming.read(65_536):
                        copied += len(chunk)
                        if copied > metadata.st_size:
                            raise GitError("snapshot_source_changed_during_copy")
                        outgoing.write(chunk)
                    if copied != metadata.st_size:
                        raise GitError("snapshot_source_changed_during_copy")
            else:
                raise GitError("snapshot_contains_nonregular_file")


def find_regression_boundary(
    repository: GitRepository,
    good_commit: str,
    bad_commit: str,
    predicate: Callable[[str], CommitOutcome | str],
    *,
    max_commits: int = 128,
) -> BisectResult:
    """Validate every bounded first-parent observation, rejecting ambiguous histories.

    A complete scan is intentional for initial small histories: a binary search
    alone cannot establish that an unobserved interval is monotonic.
    """
    commits = repository.commits_between(good_commit, bad_commit, limit=max_commits)
    observations: list[tuple[str, CommitOutcome]] = []
    for commit in commits:
        outcome = predicate(commit)
        if outcome not in {"pass", "fail"}:
            raise GitError("history_probe_inconclusive")
        observations.append((commit, "pass" if outcome == "pass" else "fail"))
    states = [outcome for _, outcome in observations]
    if states[0] != "pass" or states[-1] != "fail":
        raise GitError("history_endpoints_do_not_bracket_a_regression")
    first_failure = states.index("fail")
    if "pass" in states[first_failure:]:
        raise GitError("history_observed_nonmonotonic")
    return BisectResult(commits[first_failure - 1], commits[first_failure], tuple(observations))
