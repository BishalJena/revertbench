"""The model file surface reads bounded project data and edits only its test."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from revertbench.harness.runtime import _bridge_module

_BRIDGE = _bridge_module()
file_tool = _BRIDGE.file_tool
model_reply = _BRIDGE.model_reply
preview_nodes = _BRIDGE.preview_nodes


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "package").mkdir()
    (tmp_path / "package/api.py").write_text("first\nneedle here\nlast\n")
    (tmp_path / ".env").write_text("SYNTHETIC_PRIVATE_MARKER")
    return tmp_path


def test_files_can_be_listed_read_and_literally_searched(project: Path) -> None:
    assert file_tool(project, "list_dir", {"path": "."})["entries"] == ["package/"]
    assert (
        file_tool(project, "read_file", {"path": "package/api.py", "start": 2, "end": 2})["content"]
        == "needle here\n"
    )
    reply = file_tool(project, "search", {"pattern": "needle", "path": "."})
    assert reply["matches"] == [{"path": "package/api.py", "line": 2, "text": "needle here"}]
    assert file_tool(project, "search", {"pattern": ".*", "path": "."})["matches"] == []


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", ".env", ".git/config", "a/../b"])
def test_private_or_escaping_paths_are_not_read(project: Path, path: str) -> None:
    assert file_tool(project, "read_file", {"path": path}) == {
        "ok": False,
        "error": "invalid_path",
    }


def test_symlink_and_hardlink_targets_are_never_followed(project: Path, tmp_path: Path) -> None:
    sentinel = tmp_path.parent / f"{tmp_path.name}-outside"
    sentinel.write_text("OUTSIDE_SENTINEL")
    (project / "link").symlink_to(sentinel)
    (project / "hard").hardlink_to(sentinel)
    for path in ("link", "hard"):
        assert file_tool(project, "read_file", {"path": path})["ok"] is False
    assert "OUTSIDE_SENTINEL" not in json.dumps(
        file_tool(project, "search", {"pattern": "SENTINEL"})
    )


def test_write_only_replaces_the_fixed_regular_submission(project: Path) -> None:
    first = "def test_example():\n    assert True\n"
    assert file_tool(project, "write_test", {"content": first})["ok"] is True
    test = project / "tests/test_revertbench_submission.py"
    assert test.read_text() == first
    assert file_tool(project, "write_test", {"content": "second"})["ok"] is True
    assert test.read_text() == "second"
    assert (
        file_tool(project, "write_test", {"content": "x", "path": "package/api.py"})["ok"] is False
    )
    test.unlink()
    test.symlink_to(project / "package/api.py")
    assert file_tool(project, "write_test", {"content": "bad"})["ok"] is False
    assert (project / "package/api.py").read_text().startswith("first")


@pytest.mark.parametrize("arguments", [{"start": True}, {"start": 0}, {"start": 1, "end": 9000}])
def test_ranges_are_strict_and_bounded(project: Path, arguments: dict[str, object]) -> None:
    assert file_tool(project, "read_file", {"path": "package/api.py", **arguments})["ok"] is False


def test_node_selection_cannot_become_a_path_flag_or_other_test() -> None:
    assert preview_nodes([]) == ["tests/test_revertbench_submission.py"]
    assert preview_nodes(["tests/test_revertbench_submission.py::test_value"])
    for nodes in (["--override-ini=x"], ["tests/test_other.py"], ["../test.py"], [True]):
        with pytest.raises(ValueError):
            preview_nodes(nodes)


def test_model_reply_caps_serialized_utf8_not_only_raw_strings() -> None:
    reply = model_reply({"ok": True, "stdout": '\u0001"雪' * 9000, "stderr": "tail"})
    assert len(json.dumps(reply, ensure_ascii=False).encode()) <= 8192
    assert reply["truncated"] is True
    assert reply["ok"] is True


def test_search_releases_descriptors_and_skips_binary_files(project: Path) -> None:
    import os

    for number in range(180):
        (project / f"file-{number}.txt").write_text("match\n")
    (project / "aaa.bin").write_bytes(b"\xff\xfe")
    before = len(os.listdir("/dev/fd"))
    result = file_tool(project, "search", {"pattern": "not present"})
    assert result["ok"] is True
    assert result["matches"] == []
    assert len(os.listdir("/dev/fd")) == before


def test_large_submission_rehydration_stays_under_exec_argument_limit(
    project: Path,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    worker = _BRIDGE.EpisodeWorker(project, project, tmp_path, tmp_path, "opaque")

    class Box:
        def exec(self, argv: list[str], **_kwargs: object) -> object:
            assert sum(len(arg.encode()) for arg in argv) <= 65536
            reply = file_tool(project, argv[-2], json.loads(argv[-1]))
            return SimpleNamespace(
                status="completed", exit_code=0, stdout=json.dumps(reply).encode()
            )

    worker.box = Box()
    content = "#" + 'mixed-雪\\"' * 4700
    assert len(content.encode()) < 65536
    worker._write_submission(content.encode())
    assert (project / "tests/test_revertbench_submission.py").read_text() == content


@pytest.fixture
def feedback_worker(project: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    worker = _BRIDGE.EpisodeWorker(project, project, project, project, "opaque")

    class BadOnly:
        bad_tree = project

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"Policy feedback accessed hidden input {name}")

    class NoPreview:
        def exec(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Inadmissible submission reached pytest")

    worker.built = BadOnly()
    worker.box = NoPreview()
    worker.metadata = {"case_id": "HIDDEN_CASE_FIX", "reference": "HIDDEN_REFERENCE"}
    monkeypatch.setattr(worker, "_new_box", lambda: None)
    return worker


def test_saved_write_is_successful_even_when_bad_only_policy_rejects(
    feedback_worker: Any,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from revertbench.grade.policy import PolicyResult

    observed = []

    def checker(source: str, bad_tree: Path) -> PolicyResult:
        observed.append((source, bad_tree))
        return PolicyResult(
            False, "unsupported_import", ("Import is outside the supported subset",)
        )

    monkeypatch.setattr("revertbench.grade.policy.check_submission", checker)
    source = "import os\ndef test_behavior():\n    assert True\n"
    reply = feedback_worker.tool("write_test", {"content": source})
    assert reply["ok"] is True
    assert reply["policy"] == {
        "allowed": False,
        "reason": "unsupported_import",
        "details": ["Import is outside the supported subset"],
    }
    assert feedback_worker.submission == source.encode()
    assert (project / "submission.py").read_text() == source
    assert observed == [(source, project)]
    preview = feedback_worker.tool("run_tests", {"node_ids": []})
    assert preview["status"] == "policy_rejected"
    assert preview["executed"] is False
    assert preview["policy"] == reply["policy"]
    assert feedback_worker.submission == source.encode()
    assert observed == [(source, project), (source, project)]


def test_admitted_bad_only_preview_keeps_policy_feedback_and_real_output(
    feedback_worker: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from revertbench.grade.policy import PolicyResult

    monkeypatch.setattr(
        "revertbench.grade.policy.check_submission", lambda *_args: PolicyResult(True, None)
    )
    feedback_worker.submission = b"def test_behavior():\n    assert False\n"
    feedback_worker.box = SimpleNamespace(
        exec=lambda *_args, **_kwargs: SimpleNamespace(
            status="completed", exit_code=1, stdout_text="AssertionError", stderr_text=""
        )
    )
    reply = feedback_worker.tool("run_tests", {"node_ids": []})
    assert reply["executed"] is True
    assert reply["exit_code"] == 1 and reply["stdout"] == "AssertionError"
    assert reply["policy"] == {"allowed": True, "reason": None, "details": []}


@pytest.mark.parametrize("failure", ["project_unavailable", "io_error"])
def test_policy_analysis_infrastructure_fault_is_not_a_model_rejection(
    feedback_worker: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from revertbench.grade.policy import PolicyResult

    def checker(*_args: Any) -> PolicyResult:
        if failure == "io_error":
            raise OSError("/Users/private/HIDDEN_REFERENCE")
        return PolicyResult(False, "project_unavailable", ("Cannot read bad source",))

    monkeypatch.setattr("revertbench.grade.policy.check_submission", checker)
    source = "def test_behavior():\n    assert True\n"
    with pytest.raises(RuntimeError, match="policy_analysis_unavailable"):
        feedback_worker.tool("write_test", {"content": source})
    assert feedback_worker.submission == source.encode()
    with pytest.raises(RuntimeError, match="policy_analysis_unavailable"):
        feedback_worker.tool("run_tests", {"node_ids": []})


def test_policy_feedback_hides_paths_and_preserves_saved_success_with_utf8_cap(
    feedback_worker: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from revertbench.grade.policy import PolicyResult

    details = ("Unsupported call /Users/private folder/HIDDEN_CASE_FIX/HIDDEN_REFERENCE.py",) + (
        '雪\\"\x01' * 5000,
    ) * 12
    monkeypatch.setattr(
        "revertbench.grade.policy.check_submission",
        lambda *_args: PolicyResult(False, "unsupported_call", details),
    )
    reply = feedback_worker.tool(
        "write_test", {"content": "def test_behavior():\n    assert True\n"}
    )
    raw = json.dumps(reply, ensure_ascii=False).encode()
    assert len(raw) <= 8192
    assert reply["ok"] is True and reply["policy"]["allowed"] is False
    assert reply["policy"]["reason"] == "unsupported_call"
    assert all(
        secret not in raw for secret in (b"HIDDEN_CASE_FIX", b"HIDDEN_REFERENCE", b"/Users/")
    )
    assert reply["policy"]["truncated"] is True


def test_policy_control_character_expansion_cannot_turn_saved_ok_into_false(
    feedback_worker: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from revertbench.grade.policy import PolicyResult

    monkeypatch.setattr(
        "revertbench.grade.policy.check_submission",
        lambda *_args: PolicyResult(False, "unsupported_call", ("\x01" * 9000,) * 8),
    )
    reply = feedback_worker.tool(
        "write_test", {"content": "def test_behavior():\n    assert True\n"}
    )
    assert reply["ok"] is True and reply["policy"]["allowed"] is False
    assert len(json.dumps(reply, ensure_ascii=False).encode()) <= 8192
