"""Canonical source transfer and bounded single-file extraction without host untar."""

from __future__ import annotations

import io
import os
import stat
import tarfile
from collections.abc import Buffer, Iterable
from itertools import islice
from pathlib import Path, PurePosixPath

SUBMISSION_PATH = "tests/test_revertbench_submission.py"
SUBMISSION_NAME = "test_revertbench_submission.py"
MAX_SUBMISSION_BYTES = 65_536
_TRANSPORT_OVERHEAD = 16_384
_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        ".ssh",
        ".aws",
        ".config",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "context",
        "cache",
        "caches",
        "runs",
        "repos",
        "artifacts",
        "trajectories",
        "scratch",
        "notes",
    }
)
_EXCLUDED_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "secrets",
        "secrets.json",
        "auth.json",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        ".coverage",
        "coverage.xml",
        "models.toml",
    }
)


class ArchiveError(ValueError):
    """A source snapshot or returned file violates the transfer contract."""


class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer)
        if self.tell() + view.nbytes > self.limit:
            raise ArchiveError("Canonical archive exceeds its byte limit")
        return super().write(view)


def _safe_name(name: str) -> PurePosixPath:
    try:
        encoded = name.encode("utf-8")
    except UnicodeError as error:
        raise ArchiveError("Archive path must be UTF-8") from error
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or len(path.parts) > 64
        or path.is_absolute()
        or path.as_posix() != name
        or len(encoded) > 240
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or character in "\\:" for character in name)
    ):
        raise ArchiveError("Archive path must be a canonical relative POSIX path")
    try:
        tarfile.TarInfo(name).tobuf(format=tarfile.USTAR_FORMAT, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError) as error:
        raise ArchiveError("Archive path cannot be represented canonically") from error
    return path


def _excluded(path: PurePosixPath) -> bool:
    for part in path.parts:
        lower = part.lower()
        if (
            lower in _EXCLUDED_PARTS
            or lower in _EXCLUDED_NAMES
            or lower.startswith(".env")
            or ".local." in lower
            or lower.endswith(
                (".pem", ".key", ".p12", ".pfx", ".pyc", ".sqlite", ".sqlite3", ".db")
            )
        ):
            return True
    return False


def _header(name: str, *, directory: bool, writable: bool, uid: int, gid: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = (0o755 if writable else 0o555) if directory else (0o644 if writable else 0o444)
    info.uid, info.gid = uid, gid
    info.uname = info.gname = ""
    info.mtime = 0
    return info


class _TreeWriter:
    def __init__(
        self,
        archive: tarfile.TarFile,
        *,
        max_file_bytes: int,
        max_files: int,
        writable: bool,
        uid: int,
        gid: int,
    ) -> None:
        self.archive = archive
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.writable, self.uid, self.gid = writable, uid, gid
        self.count = 0

    def add(self, path: Path, name: str) -> None:
        self.count += 1
        if self.count > self.max_files:
            raise ArchiveError("Source tree exceeds its entries limit")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ArchiveError("Source symbolic links are forbidden")
        directory = stat.S_ISDIR(metadata.st_mode)
        if not directory and not stat.S_ISREG(metadata.st_mode):
            raise ArchiveError("Only regular source files and directories are supported")
        if metadata.st_nlink > 1 and not directory:
            raise ArchiveError("Source hardlinks are forbidden")
        info = _header(
            name, directory=directory, writable=self.writable, uid=self.uid, gid=self.gid
        )
        if directory:
            self.archive.addfile(info)
            self._children(path, name)
        else:
            self._file(path, info)

    def _children(self, directory: Path, prefix: str) -> None:
        children = list(islice(directory.iterdir(), self.max_files + 1))
        if len(children) > self.max_files:
            raise ArchiveError("Source directory exceeds its entries limit")
        for path in sorted(children, key=lambda candidate: candidate.name):
            relative = path.name if prefix == "." else f"{prefix}/{path.name}"
            safe = _safe_name(relative)
            if _excluded(safe):
                continue
            if relative == SUBMISSION_PATH:
                raise ArchiveError("Source tree already contains the reserved submission path")
            self.add(path, relative)

    def _file(self, path: Path, info: tarfile.TarInfo) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ArchiveError("Source file must be regular without links")
            if metadata.st_size > self.max_file_bytes:
                raise ArchiveError("Source file exceeds its byte limit")
            data = stream.read(self.max_file_bytes + 1)
            if len(data) > self.max_file_bytes:
                raise ArchiveError("Source file grew past its byte limit")
            info.size = len(data)
            self.archive.addfile(info, io.BytesIO(data))


def archive_tree(
    tree: Path,
    *,
    writable: bool = False,
    max_bytes: int = 16 * 1024 * 1024,
    max_file_bytes: int = 4 * 1024 * 1024,
    max_files: int = 4096,
    uid: int = 10001,
    gid: int = 10001,
) -> bytes:
    """Snapshot a trusted case directory, excluding known history/private artifacts.

    Metadata is deterministic. The caller must select a case tree, not arbitrary
    personal data: filename exclusions cannot discover every embedded credential.
    ``max_files`` bounds all archive entries, including directories and the root.
    """
    if min(max_bytes, max_file_bytes, max_files) <= 0 or min(uid, gid) < 0:
        raise ArchiveError("Archive limits and owner IDs must be valid positive values")
    try:
        if tree.is_symlink() or not tree.is_dir():
            raise ArchiveError("Source root must be a real directory")
        buffer = _BoundedBuffer(max_bytes)
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            writer = _TreeWriter(
                archive,
                max_file_bytes=max_file_bytes,
                max_files=max_files,
                writable=writable,
                uid=uid,
                gid=gid,
            )
            writer.add(tree, ".")
        return buffer.getvalue()
    except (OSError, tarfile.TarError, UnicodeError) as error:
        raise ArchiveError("Source tree cannot be archived safely") from error


def single_file_archive(
    data: bytes,
    *,
    name: str = SUBMISSION_PATH,
    writable: bool = False,
    uid: int = 10001,
    gid: int = 10001,
    max_bytes: int = MAX_SUBMISSION_BYTES,
) -> bytes:
    """Build one canonical regular-file archive for a bounded trusted byte payload."""
    safe = _safe_name(name)
    if _excluded(safe):
        raise ArchiveError("Private artifact paths cannot be transferred")
    if len(data) > max_bytes or max_bytes <= 0 or min(uid, gid) < 0:
        raise ArchiveError("File payload exceeds its size or owner limit")
    buffer = _BoundedBuffer(max_bytes + _TRANSPORT_OVERHEAD)
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = _header(name, directory=False, writable=writable, uid=uid, gid=gid)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _collect(stream: bytes | Iterable[bytes], limit: int) -> bytes:
    if isinstance(stream, bytes):
        if len(stream) > limit:
            raise ArchiveError("Archive transport exceeds its byte limit")
        return stream
    output = bytearray()
    try:
        for chunk in stream:
            if not isinstance(chunk, bytes):
                raise ArchiveError("Archive transport must yield bytes")
            if not chunk:
                raise ArchiveError("Archive transport yielded an empty chunk")
            if len(output) + len(chunk) > limit:
                raise ArchiveError("Archive transport exceeds its byte limit")
            output.extend(chunk)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return bytes(output)


def _read_member(data: bytes, expected_name: str, max_bytes: int) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        member = archive.next()
        if member is None or member.name != expected_name:
            raise ArchiveError("Archive must contain the single expected submission file")
        _safe_name(member.name)
        if (
            member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
            or member.sparse is not None
            or member.linkname
        ):
            raise ArchiveError("Submission must be a single regular file without links")
        if member.size < 0 or member.size > max_bytes:
            raise ArchiveError("Submission exceeds its file size limit")
        stream = archive.extractfile(member)
        if stream is None:
            raise ArchiveError("Submission is not a readable regular file")
        with stream:
            payload = stream.read(max_bytes + 1)
        if len(payload) != member.size:
            raise ArchiveError("Submission file size is inconsistent or truncated")
        if archive.next() is not None:
            raise ArchiveError("Archive must contain a single file without duplicates")
        end = member.offset_data + ((member.size + 511) // 512) * 512
        if len(data) % 512 or len(data) < end + 1024 or any(data[end:]):
            raise ArchiveError("Archive has a missing trailer or unexpected trailing data")
        return payload


def extract_submission(
    archive: bytes | Iterable[bytes],
    *,
    expected_name: str = SUBMISSION_NAME,
    max_bytes: int = MAX_SUBMISSION_BYTES,
) -> bytes:
    """Read exactly one bounded UTF-8 regular file; never extract onto the host.

    The full uncompressed transport is bounded before tar parsing, and extra
    members, links, traversal variants, compressed input and hidden tails fail.
    """
    _safe_name(expected_name)
    if max_bytes <= 0:
        raise ArchiveError("Submission size limit must be positive")
    data = _collect(archive, max_bytes + _TRANSPORT_OVERHEAD)
    try:
        payload = _read_member(data, expected_name, max_bytes)
        payload.decode("utf-8")
    except UnicodeError as error:
        raise ArchiveError("Submission must contain valid UTF-8") from error
    except (tarfile.TarError, OSError, ValueError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError("Submission archive is malformed") from error
    return payload
