"""Archive boundary tests never extract untrusted tar files onto the host."""

import io
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from revertbench.sandbox.archives import (
    ArchiveError,
    archive_tree,
    extract_submission,
    single_file_archive,
)


def _tar(*members: tuple[str, bytes, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data, member_type in members:
            info = tarfile.TarInfo(name)
            info.type = member_type
            info.size = len(data) if member_type == tarfile.REGTYPE else 0
            if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                info.linkname = "target.py"
            archive.addfile(info, io.BytesIO(data) if info.size else None)
    return buffer.getvalue()


def test_tree_archive_is_canonical_and_removes_private_artifacts(tmp_path: Path) -> None:
    tree = tmp_path / "case"
    tree.mkdir()
    for relative in (
        "pkg.py",
        "tests/test_api.py",
        ".git/config",
        "nested/.git/objects/secret",
        ".env",
        ".env.development",
        ".envrc",
        "notes/private.md",
        "provider.sqlite3",
        "context/DESIGN.md",
        "configs/models.toml",
        "__pycache__/pkg.pyc",
        ".aws/credentials",
        "private.pem",
        "session.local.toml",
    ):
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public" if relative in {"pkg.py", "tests/test_api.py"} else "private")
    first = archive_tree(tree)
    os.chmod(tree / "pkg.py", 0o777)
    os.utime(tree / "pkg.py", (1234567, 7654321))
    second = archive_tree(tree)

    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:") as archive:
        members = archive.getmembers()
        assert {member.name for member in members} == {
            ".",
            "pkg.py",
            "tests",
            "tests/test_api.py",
            "nested",
            "configs",
        }
        for member in members:
            assert member.uid == member.gid == 10001
            assert member.mtime == 0
            assert member.uname == member.gname == ""
            assert member.mode == (0o555 if member.isdir() else 0o444)


def test_writable_archive_has_editable_normalized_permissions(tmp_path: Path) -> None:
    (tmp_path / "pkg.py").write_text("x = 1")
    with tarfile.open(
        fileobj=io.BytesIO(archive_tree(tmp_path, writable=True)), mode="r:"
    ) as archive:
        assert archive.getmember(".").mode == 0o755
        assert archive.getmember("pkg.py").mode == 0o644


@pytest.mark.parametrize("name", ["linked.py", "linked_dir"])
def test_source_symlinks_are_rejected(tmp_path: Path, name: str) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / name).symlink_to(outside)

    with pytest.raises(ArchiveError, match="link"):
        archive_tree(tmp_path)


def test_source_hardlinks_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "file.py").write_text("pass")
    os.link(tmp_path / "file.py", tmp_path / "alias.py")

    with pytest.raises(ArchiveError, match="link"):
        archive_tree(tmp_path)


def test_special_source_files_are_rejected(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")

    with pytest.raises(ArchiveError, match="regular"):
        archive_tree(tmp_path)


@pytest.mark.parametrize("name", ["strange\\path.py", "C:secret.py", "control\nname.py"])
def test_ambiguous_source_names_are_rejected(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("pass")

    with pytest.raises(ArchiveError, match="path"):
        archive_tree(tmp_path)


def test_existing_reserved_submission_is_rejected(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_revertbench_submission.py").write_text("assert True")

    with pytest.raises(ArchiveError, match="reserved"):
        archive_tree(tmp_path)


def test_source_limits_bound_files_count_and_total_archive(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_bytes(b"x" * 1000)
    with pytest.raises(ArchiveError, match="file"):
        archive_tree(tmp_path, max_file_bytes=100)
    with pytest.raises(ArchiveError, match="entries"):
        archive_tree(tmp_path, max_files=1)
    with pytest.raises(ArchiveError, match="archive"):
        archive_tree(tmp_path, max_bytes=1024)


def test_source_root_must_be_a_real_directory(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        archive_tree(tmp_path / "missing")
    path = tmp_path / "link"
    path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArchiveError):
        archive_tree(path)


def test_extracts_exact_regular_submission_from_bounded_stream() -> None:
    payload = b"def test_behavior():\n    assert True\n"
    archive = _tar(("test_revertbench_submission.py", payload, tarfile.REGTYPE))

    assert extract_submission(archive) == payload
    assert extract_submission(archive[i : i + 17] for i in range(0, len(archive), 17)) == payload


@pytest.mark.parametrize(
    "name",
    [
        "../test_revertbench_submission.py",
        "/test_revertbench_submission.py",
        "./test_revertbench_submission.py",
        "sub/../test_revertbench_submission.py",
        "C:test_revertbench_submission.py",
        "test_revertbench_submission.py/",
    ],
)
def test_submission_path_variations_are_rejected(name: str) -> None:
    with pytest.raises(ArchiveError):
        extract_submission(_tar((name, b"assert True", tarfile.REGTYPE)))


@pytest.mark.parametrize(
    "member_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE],
)
def test_submission_links_and_special_members_are_rejected(member_type: bytes) -> None:
    with pytest.raises(ArchiveError, match="regular"):
        extract_submission(_tar(("test_revertbench_submission.py", b"", member_type)))


def test_extra_or_duplicate_members_are_rejected() -> None:
    member = ("test_revertbench_submission.py", b"assert True", tarfile.REGTYPE)
    with pytest.raises(ArchiveError, match="single"):
        extract_submission(_tar(member, member))
    with pytest.raises(ArchiveError):
        extract_submission(_tar(member, ("conftest.py", b"pass", tarfile.REGTYPE)))


def test_submission_size_and_encoding_are_checked() -> None:
    with pytest.raises(ArchiveError, match="size"):
        extract_submission(
            _tar(("test_revertbench_submission.py", b"x" * 101, tarfile.REGTYPE)), max_bytes=100
        )
    with pytest.raises(ArchiveError, match="UTF-8"):
        extract_submission(_tar(("test_revertbench_submission.py", b"\xff", tarfile.REGTYPE)))


def test_transport_limit_closes_stream_before_tar_parsing() -> None:
    closed: list[bool] = []

    def endless() -> Iterator[bytes]:
        try:
            while True:
                yield b"x" * 1000
        finally:
            closed.append(True)

    with pytest.raises(ArchiveError, match="transport"):
        extract_submission(endless(), max_bytes=100)
    assert closed == [True]


def test_invalid_empty_compressed_and_trailing_archives_are_rejected() -> None:
    member = ("test_revertbench_submission.py", b"assert True", tarfile.REGTYPE)
    valid = _tar(member)
    for malformed in (b"", b"not tar", valid[:512], valid[:-1], valid + _tar(member)):
        with pytest.raises(ArchiveError):
            extract_submission(malformed)
    compressed = io.BytesIO()
    with tarfile.open(fileobj=compressed, mode="w:gz") as archive:
        info = tarfile.TarInfo(member[0])
        info.size = 11
        archive.addfile(info, io.BytesIO(b"assert True"))
    with pytest.raises(ArchiveError):
        extract_submission(compressed.getvalue())


def test_single_file_builder_is_canonical_and_rejects_unsafe_names() -> None:
    payload = b"assert True\n"
    archive = single_file_archive(payload, name="test_revertbench_submission.py")
    assert extract_submission(archive) == payload
    assert archive == single_file_archive(payload, name="test_revertbench_submission.py")
    for name in ("../bad", "/bad", ".env", "context/DESIGN.md"):
        with pytest.raises(ArchiveError):
            single_file_archive(payload, name=name)


def test_named_report_transport_reuses_the_same_regular_file_boundary() -> None:
    payload = b'{"complete":true}'
    archive = _tar(("result.json", payload, tarfile.REGTYPE))
    assert extract_submission(archive, expected_name="result.json", max_bytes=1024) == payload


@pytest.mark.parametrize("name", [".", "a" * 101, "a/" * 80 + "leaf.py"])
def test_unrepresentable_or_root_file_names_fail_closed(name: str) -> None:
    with pytest.raises(ArchiveError, match="path"):
        single_file_archive(b"pass", name=name)


def test_empty_transport_chunks_cannot_create_an_unbounded_read_loop() -> None:
    def empties() -> Iterator[bytes]:
        yield b""
        raise AssertionError("Transport continued after an empty chunk")

    with pytest.raises(ArchiveError, match="transport"):
        extract_submission(empties())


def test_regular_member_cannot_smuggle_link_metadata() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("test_revertbench_submission.py")
        member.type = tarfile.REGTYPE
        member.linkname = "/elsewhere"
        member.size = 4
        archive.addfile(member, io.BytesIO(b"pass"))
    with pytest.raises(ArchiveError, match="regular"):
        extract_submission(buffer.getvalue())
