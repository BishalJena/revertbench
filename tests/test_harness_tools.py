"""The model file surface reads bounded project data and edits only its test."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_bounded_tools", Path(__file__).resolve().parents[1] / "scripts/episode_runtime.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_BRIDGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BRIDGE)
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
