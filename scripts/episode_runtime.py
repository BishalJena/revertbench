"""Trusted episode bridge, executed under an accepted case's original UV snapshot.

This file stays outside the fingerprinted package. Generated Python executes only
inside AgentSandbox/DockerRunner, never in this host interpreter.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import re
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any

SUBMISSION = "tests/test_revertbench_submission.py"
MAX_TOOL_BYTES = 8192


def reproduction_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_episode_reproduction", Path(__file__).with_name("reproduce_case.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("reproduction_helper_missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


_IDENTITY = r"""
import hashlib,json,sys
from pathlib import Path
root=Path('/opt/revertbench/src/revertbench'); digest=hashlib.sha256()
for path in sorted(root.rglob('*.py')):
 name=path.relative_to(root).as_posix().encode(); data=path.read_bytes()
 digest.update(len(name).to_bytes(8,'big')+name); digest.update(len(data).to_bytes(8,'big')+data)
print(json.dumps(dict(python='.'.join(map(str,sys.version_info[:3])),
 dependency_lock_sha256=hashlib.sha256(Path('/opt/revertbench/uv.lock').read_bytes()).hexdigest(),
 grader_sha256=digest.hexdigest(),policy_sha256=hashlib.sha256((root/'grade/policy.py').read_bytes()).hexdigest())))
"""

_PREVIEW = r"""
import json,os,sys
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'; os.environ['PYTEST_ADDOPTS']=''
import pytest
sys.path[:0]=['/workspace/repo/src','/workspace/repo']
args=['-q','-s','--disable-warnings','-c','/opt/revertbench/pytest.ini',
 '--rootdir','/workspace/repo','--noconftest','-p','no:cacheprovider']
raise SystemExit(pytest.main(args+json.loads(sys.argv[1])))
"""


class EpisodeWorker:
    """Pinned host controller. Model operations never receive its private paths."""

    def __init__(
        self, case_dir: Path, pinned_root: Path, cache: Path, output: Path, episode_id: str
    ) -> None:
        self.case_dir, self.pinned_root = case_dir, pinned_root
        self.cache, self.output, self.episode_id = cache, output, episode_id
        self.client: Any = None
        self.box: Any = None
        self.built: Any = None
        self.image = ""
        self.submission: bytes | None = None
        self.temporary: TemporaryDirectory[str] | None = None
        self.metadata: dict[str, Any] | None = None

    def prepare(self) -> dict[str, Any]:
        import docker

        import revertbench
        from revertbench.build import materialize_candidate
        from revertbench.mine.git import GitRepository
        from revertbench.mine.models import candidate_from_json
        from revertbench.review import _machine_passed, build_review_packet, load_case
        from revertbench.sandbox import image as image_module
        from revertbench.validate.identity import host_code_identity

        if self.metadata is not None:
            return self.metadata
        if Path(revertbench.__file__).resolve().parent != self.pinned_root / "src/revertbench":
            raise RuntimeError("pinned_import_mismatch")
        helper = reproduction_module()
        publication = helper.read_publication(self.case_dir)
        raw_case = json.loads((self.case_dir / "case.json").read_bytes())
        if not helper._has_explicit_acceptance(raw_case):
            raise RuntimeError("case_not_accepted")
        case = load_case(self.case_dir / "case.json")
        build_review_packet(self.case_dir / "case.json")
        _machine_passed(case, self.case_dir)
        if (
            case.env is None
            or case.candidate_artifact is None
            or case.reference_test is None
            or case.global_suite_evidence is None
            or case.bad_pr is None
            or case.bad_diff is None
        ):
            raise RuntimeError("case_inputs_missing")
        if host_code_identity() != (case.validation.grader_sha256, case.validation.policy_sha256):
            raise RuntimeError("pinned_code_mismatch")
        lock = hashlib.sha256((self.pinned_root / "uv.lock").read_bytes()).hexdigest()
        if lock != case.env.dependency_lock_sha256:
            raise RuntimeError("pinned_lock_mismatch")
        candidate = candidate_from_json((self.case_dir / case.candidate_artifact.path).read_text())
        repository = GitRepository.clone(case.repo, self.cache / "repos")
        self.temporary = TemporaryDirectory(prefix="episode-trees-", dir=self.cache)
        self.built = materialize_candidate(
            repository, candidate, Path(self.temporary.name) / "case"
        )
        proof, built_proof = (
            getattr(case, "repair_derivation", None),
            getattr(self.built, "repair_derivation", None),
        )
        if (
            self.built.case_id != case.id
            or self.built.source_hashes != case.source_hashes
            or self.built.changed_lines != case.changed_lines
            or self.built.patch_sha256 != case.repair.patch_sha256
            or hashlib.sha256(self.built.reference_source.encode()).hexdigest()
            != case.reference_test.source_sha256
            or (None if proof is None else proof.content_digest())
            != (None if built_proof is None else built_proof.content_digest())
        ):
            raise RuntimeError("case_reconstruction_mismatch")
        image_module._BASE_IMAGE = publication["build"]["base_image"]
        self.client = docker.from_env(timeout=15)
        self.client.ping()
        self.image = image_module.build_worker_image(self.client)
        self._new_box()
        observed = self.box.exec(
            ["/usr/local/bin/python", "-I", "-S", "-c", _IDENTITY], output_limit_bytes=4096
        )
        if observed.status != "completed" or observed.exit_code != 0:
            raise RuntimeError("image_identity_unavailable")
        identity = json.loads(observed.stdout)
        expected = {
            "python": case.env.python,
            "dependency_lock_sha256": lock,
            "grader_sha256": case.validation.grader_sha256,
            "policy_sha256": case.validation.policy_sha256,
        }
        if identity != expected:
            raise RuntimeError("image_identity_mismatch")
        actual_image = self.client.images.get(self.image)
        base = self.client.images.get(publication["build"]["base_image"])
        layers = base.attrs.get("RootFS", {}).get("Layers", [])
        if (
            not layers
            or actual_image.attrs.get("RootFS", {}).get("Layers", [])[: len(layers)] != layers
        ):
            raise RuntimeError("image_base_mismatch")
        self.metadata = {
            **identity,
            "image": self.image,
            "approved_image": case.env.image,
            "base_image": publication["build"]["base_image"],
            "architecture": actual_image.attrs.get("Architecture"),
            "image_context_sha256": actual_image.labels.get("revertbench.worker.context"),
            "case_id": case.id,
            "case_inputs_sha256": case.review_input_digest(),
            "case_sha256": hashlib.sha256((self.case_dir / "case.json").read_bytes()).hexdigest(),
            "implementation_commit": case.global_suite_evidence.implementation_commit,
            "source_hashes": case.source_hashes,
            "patch_sha256": case.repair.patch_sha256,
            "repair_derivation_sha256": None if proof is None else proof.content_digest(),
            "body_policy": "omitted_all_cases_v1",
            "safe_task": {
                "episode_id": self.episode_id,
                "bad_pr_title": case.bad_pr.title,
                "bad_diff": (self.case_dir / case.bad_diff.path).read_text(),
            },
        }
        return self.metadata

    def _new_box(self) -> None:
        from revertbench.sandbox.agent import AgentSandbox

        if self.box is not None:
            self.box.close()
            self.box = None
        self.box = AgentSandbox(self.image, self.built.bad_tree, client=self.client).start()
        if self.submission is not None:
            self._write_submission(self.submission)

    def _write_submission(self, data: bytes) -> None:
        if self._files("write_test", {"content": ""}).get("ok") is not True:
            raise RuntimeError("submission_restore_failed")
        text = data.decode("utf-8")
        for index in range(0, len(text), 4096):
            result = self._files("append_test_chunk", {"content": text[index : index + 4096]})
            if result.get("ok") is not True:
                raise RuntimeError("submission_restore_failed")

    def _files(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        code = (
            "from __future__ import annotations\nimport json,sys\nfrom pathlib import Path\n"
            + inspect.getsource(file_tool)
            + "\nprint(json.dumps(file_tool(Path('/workspace/repo'),sys.argv[1],"
            "json.loads(sys.argv[2])),ensure_ascii=False))"
        )
        result = self.box.exec(
            [
                "/usr/local/bin/python",
                "-I",
                "-S",
                "-c",
                code,
                name,
                json.dumps(arguments, ensure_ascii=False),
            ],
            output_limit_bytes=65536,
        )
        if result.status != "completed" or result.exit_code != 0:
            raise RuntimeError("file_tool_failed")
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict):
            raise RuntimeError("file_tool_report_invalid")
        return reply

    def restore_submission(self, data: bytes) -> None:
        if len(data) > 65536:
            raise ValueError("submission_too_large")
        data.decode("utf-8")
        self.submission = data
        target = self.output / "submission.py"
        temporary = self.output / "submission.tmp"
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        self._new_box()

    def _policy_feedback(self) -> dict[str, Any]:
        """Use only the imported pinned policy and pristine bad source, never an oracle."""
        from revertbench.grade.policy import check_submission

        if self.submission is None:
            raise RuntimeError("no_submission")
        try:
            decision = check_submission(self.submission.decode("utf-8"), self.built.bad_tree)
        except OSError as error:
            raise RuntimeError("policy_analysis_unavailable") from error
        if decision.reason == "project_unavailable":
            raise RuntimeError("policy_analysis_unavailable")
        reason = decision.reason
        if reason is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason) is None:
            reason = "policy_rejected"
        details: list[str] = []
        truncated = len(decision.details) > 4
        for detail in decision.details[:4]:
            # Keep useful admission explanations, without absolute host paths or controls.
            text = "".join(character if character.isprintable() else " " for character in detail)
            text = " ".join(text.split())
            text = re.sub(r"(?<![\w.])(?:/[^\s]|[A-Za-z]:\\).*", "<path>", text)
            raw = text.encode("utf-8", errors="replace")
            truncated |= len(raw) > 512
            details.append(raw[:512].decode("utf-8", errors="ignore"))
        feedback: dict[str, Any] = {
            "allowed": decision.allowed,
            "reason": reason,
            "details": details,
        }
        if truncated:
            feedback["truncated"] = True
        return feedback

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.built is None:
            raise RuntimeError("not_prepared")
        if name == "submit":
            if arguments:
                return {"ok": False, "error": "invalid_arguments"}
            if self.submission is None:
                return {"ok": False, "error": "no_submission"}
            self._new_box()
            if self.box.submission_bytes() != self.submission:
                raise RuntimeError("submission_identity_mismatch")
            return {"submitted": True}
        if name == "run_tests":
            if set(arguments) - {"node_ids"}:
                return {"ok": False, "error": "invalid_arguments"}
            try:
                nodes = preview_nodes(arguments.get("node_ids", []))
            except ValueError:
                return {"ok": False, "error": "invalid_node_ids"}
            if self.submission is None:
                return {"ok": False, "error": "no_submission"}
            policy = self._policy_feedback()
            if not policy["allowed"]:
                return {"status": "policy_rejected", "executed": False, "policy": policy}
            try:
                result = self.box.exec(
                    ["/usr/local/bin/python", "-I", "-c", _PREVIEW, json.dumps(nodes)],
                    timeout_seconds=10,
                    output_limit_bytes=8192,
                )
                reply = {
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout_text,
                    "stderr": result.stderr_text,
                    "truncated": result.status == "output_limit",
                    "executed": True,
                    "policy": policy,
                }
            finally:
                self._new_box()
            return model_reply(reply)
        if name not in {"list_dir", "read_file", "search", "write_test"}:
            return {"ok": False, "error": "unknown_tool"}
        if name == "write_test":
            if set(arguments) != {"content"} or not isinstance(arguments.get("content"), str):
                return {"ok": False, "error": "invalid_arguments"}
            try:
                self.restore_submission(arguments["content"].encode("utf-8"))
            except (UnicodeError, ValueError):
                return {"ok": False, "error": "invalid_content"}
            return model_reply(
                {
                    "ok": True,
                    "bytes": len(self.submission or b""),
                    "policy": self._policy_feedback(),
                }
            )
        return model_reply(self._files(name, arguments))

    def grade(self) -> dict[str, Any]:
        from revertbench.grade.rules import grade_submission
        from revertbench.sandbox.worker import DockerRunner

        if self.submission is None:
            raise RuntimeError("no_submission")
        result = grade_submission(
            self.built.bad_tree,
            self.built.repaired_tree,
            self.submission.decode("utf-8"),
            self.built.changed_lines,
            execution_backend=DockerRunner(self.image, client=self.client),
        )
        return dict(json.loads(result.model_dump_json()))

    def close(self) -> None:
        try:
            if self.box is not None:
                self.box.close()
                self.box = None
        finally:
            if self.client is not None:
                self.client.close()
                self.client = None
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("case-dir", "pinned-root", "cache", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--episode-id", required=True)
    args = parser.parse_args()
    worker = EpisodeWorker(
        args.case_dir, args.pinned_root, args.cache, args.output, args.episode_id
    )

    def stop(_signum: int, _frame: Any) -> None:
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, stop)
    try:
        while line := sys.stdin.buffer.readline(524289):
            if len(line) > 524288 or not line.endswith(b"\n"):
                raise RuntimeError("request_limit")
            request = json.loads(line)
            operation = request["operation"]
            payload = request["payload"]
            if operation == "prepare":
                result = worker.prepare()
            elif operation == "tool":
                result = worker.tool(payload["name"], payload["arguments"])
            elif operation == "restore":
                worker.restore_submission(payload["content"].encode("utf-8"))
                result = {"restored": True}
            elif operation == "grade":
                result = worker.grade()
            elif operation == "close":
                worker.close()
                result = {"closed": True}
            else:
                raise ValueError("invalid_operation")
            print(
                json.dumps(
                    {
                        "protocol": "1",
                        "episode_id": args.episode_id,
                        "request_id": request["request_id"],
                        "operation": operation,
                        "result": result,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if operation == "close":
                break
    finally:
        worker.close()


if __name__ == "__main__":
    main()
