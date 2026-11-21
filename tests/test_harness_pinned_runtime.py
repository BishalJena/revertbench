"""Real Docker proofs for oldest/latest pinned grading environments."""

import json
from pathlib import Path

import pytest

from revertbench.harness.runtime import PinnedRuntime


@pytest.mark.docker
@pytest.mark.parametrize("case_prefix", ["more-itertools-71b768", "more-itertools-2b91"])
def test_original_snapshot_grades_generated_test_and_preview_is_bad_only(
    tmp_path: Path,
    case_prefix: str,
) -> None:
    cases = Path(__file__).resolve().parents[1] / "cases"
    case_dir = next(path for path in cases.iterdir() if path.name.startswith(case_prefix))
    case = json.loads((case_dir / "case.json").read_text())
    # Maintainer reference is a trusted test fixture here, never a model prompt/tool input.
    source = (case_dir / "reference.py").read_bytes()
    with PinnedRuntime(
        case_dir, cache=tmp_path / "cache", output=tmp_path / "out", episode_id="opaque-smoke"
    ) as runtime:
        prepared = runtime.prepare()
        assert prepared["grader_sha256"] == case["validation"]["grader_sha256"]
        assert prepared["dependency_lock_sha256"] == case["env"]["dependency_lock_sha256"]
        assert case["fix_commit"] not in json.dumps(prepared["safe_task"])
        runtime.restore_submission(source)
        rejected = b"import os\ndef test_forbidden():\n    assert True\n"
        saved = runtime.tool("write_test", {"content": rejected.decode()})
        assert saved["ok"] is True and saved["policy"]["allowed"] is False
        assert runtime.get_submission() == rejected
        blocked = runtime.tool("run_tests", {"node_ids": []})
        assert blocked["status"] == "policy_rejected" and blocked["executed"] is False
        policy_result = runtime.grade()
        assert policy_result["status"] == "rejected"
        assert policy_result["score"] == 0
        assert policy_result["reason"] == "policy_rejected"
        comprehension = "def test_values():\n    assert [value for value in [1]] == [1]\n"
        eligibility = runtime.tool("write_test", {"content": comprehension})
        assert eligibility["ok"] is True
        assert eligibility["policy"]["allowed"] is ("2b91" in case_prefix)
        assert runtime.get_submission() == comprehension.encode()
        runtime.restore_submission(source)
        preview = runtime.tool("run_tests", {"node_ids": []})
        assert preview["exit_code"] == 1
        assert preview["status"] == "completed"
        assert preview["executed"] is True and preview["policy"]["allowed"] is True
        assert runtime.get_submission() == source
        if "2b91" in case_prefix:
            runtime.restore_submission(
                b"from pathlib import Path\ndef test_workspace():\n"
                b"    Path('preview-marker.txt').write_text('temporary')\n"
            )
            blocked = runtime.tool("run_tests", {"node_ids": []})
            assert blocked["status"] == "policy_rejected" and blocked["executed"] is False
            assert runtime.tool("read_file", {"path": "preview-marker.txt"}) == {
                "ok": False,
                "error": "invalid_path",
            }
            runtime.restore_submission(source)
        assert runtime.tool("submit", {}) == {"submitted": True}
        result = runtime.grade()
        assert result["status"] == "passed"
        assert result["score"] == 1
        assert len(result["executions"]) == 6
        assert all(run["seed"] in {11, 23, 37} for run in result["executions"])
