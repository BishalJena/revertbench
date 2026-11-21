"""Bounded repository tools shared by the pinned episode bridge."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SUBMISSION = "tests/test_revertbench_submission.py"


MAX_TOOL_BYTES = 8192


def model_reply(reply: dict[str, Any]) -> dict[str, Any]:
    """Cap the serialized UTF8 reply, including JSON escapes and metadata."""
    while len(json.dumps(reply, ensure_ascii=False).encode("utf-8")) > MAX_TOOL_BYTES:
        reply["truncated"] = True
        strings = [(len(value), key) for key, value in reply.items() if isinstance(value, str)]
        if strings and max(strings)[0] > 128:
            _, key = max(strings)
            reply[key] = reply[key][: len(reply[key]) // 2]
        elif isinstance(reply.get("matches"), list) and reply["matches"]:
            reply["matches"].pop()
        elif isinstance(reply.get("entries"), list) and reply["entries"]:
            reply["entries"].pop()
        else:
            return {"ok": False, "error": "tool_output_limit", "truncated": True}
    return reply


def preview_nodes(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("invalid_node_ids")
    for node in value:
        if (
            not isinstance(node, str)
            or re.fullmatch(
                r"tests/test_revertbench_submission\.py(?:::[A-Za-z_]\w*){0,2}"
                r"(?:\[[^\]\r\n]{1,128}\])?",
                node,
            )
            is None
        ):
            raise ValueError("invalid_node_ids")
    return value or [SUBMISSION]


def file_tool(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Self-contained stdlib shim; its trusted source is sent into the container."""
    import os
    import stat
    from contextlib import suppress
    from pathlib import PurePosixPath

    class BadPath(ValueError):
        pass

    descriptors: list[int] = []

    def parts(value: object) -> tuple[str, ...]:
        if not isinstance(value, str) or len(value.encode()) > 512 or "\0" in value:
            raise BadPath
        if value == ".":
            return ()
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or path.as_posix() != value
            or any(part.startswith(".") or part in {"context", "configs"} for part in path.parts)
        ):
            raise BadPath
        return path.parts

    def directory(path: tuple[str, ...], *, create: bool = False) -> int:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in path:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o755, dir_fd=fd)
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        return fd

    def read(path: tuple[str, ...]) -> tuple[str, bool]:
        if not path:
            raise BadPath
        parent = directory(path[:-1])
        fd = os.open(path[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BadPath
        raw = os.read(fd, 262145)
        return raw[:262144].decode("utf-8"), len(raw) > 262144

    allowed = {
        "list_dir": {"path"},
        "read_file": {"path", "start", "end"},
        "search": {"pattern", "path"},
        "write_test": {"content"},
        "append_test_chunk": {"content"},
    }
    if name not in allowed or set(arguments) - allowed[name]:
        return {"ok": False, "error": "invalid_arguments"}
    try:
        if name in {"write_test", "append_test_chunk"}:
            content = arguments.get("content")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 65536:
                return {"ok": False, "error": "invalid_content"}
            fd = directory(("tests",), create=True)
            target = "test_revertbench_submission.py"
            try:
                metadata = os.stat(target, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                metadata = None
            if metadata is not None and (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            ):
                raise BadPath
            if name == "append_test_chunk":
                if metadata is None or metadata.st_size + len(content.encode()) > 65536:
                    raise BadPath
                output = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(output, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                return {"ok": True}
            temporary = ".submission-" + os.urandom(12).hex()
            output = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd
            )
            try:
                with os.fdopen(output, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                os.replace(temporary, target, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=fd)
            return {"ok": True, "bytes": len(content.encode("utf-8"))}
        path = parts(arguments.get("path", "."))
        if name == "list_dir":
            fd = directory(path)
            entries = []
            for entry in sorted(os.listdir(fd)):
                if entry.startswith(".") or entry in {"context", "configs"}:
                    continue
                metadata = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    entries.append(entry + "/")
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    entries.append(entry)
            return {"ok": True, "entries": entries[:128], "truncated": len(entries) > 128}
        if name == "read_file":
            start = arguments.get("start", 1)
            end = arguments.get("end", start + 79 if type(start) is int else 0)
            if (
                type(start) is not int
                or type(end) is not int
                or not 1 <= start <= end < start + 200
            ):
                return {"ok": False, "error": "invalid_range"}
            text, truncated = read(path)
            lines = text.splitlines(keepends=True)
            return {
                "ok": True,
                "content": "".join(lines[start - 1 : end])[:8192],
                "start": start,
                "end": min(end, len(lines)),
                "truncated": truncated or end < len(lines),
            }
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not 1 <= len(pattern.encode()) <= 256:
            return {"ok": False, "error": "invalid_pattern"}
        pending = [path]
        matches: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        while pending and scanned < 512 and len(matches) < 32:
            current = pending.pop()
            try:
                fd = directory(current)
            except NotADirectoryError:
                try:
                    text, clipped = read(current)
                except UnicodeError:
                    continue
                scanned += 1
                truncated |= clipped
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern in line:
                        matches.append(
                            {"path": "/".join(current), "line": number, "text": line[:512]}
                        )
                        if len(matches) == 32:
                            truncated = True
                            break
            else:
                for entry in reversed(sorted(os.listdir(fd))):
                    if entry.startswith(".") or entry in {"context", "configs"}:
                        continue
                    metadata = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode) or (
                        stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                    ):
                        pending.append((*current, entry))
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
                descriptors.clear()
        return {"ok": True, "matches": matches, "truncated": truncated or bool(pending)}
    except (BadPath, OSError):
        return {"ok": False, "error": "invalid_path"}
    except (UnicodeError, ValueError, TypeError):
        return {"ok": False, "error": "invalid_arguments"}
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
