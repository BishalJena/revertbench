"""Model context is deliberately constructed without case or repair metadata."""

import json

import pytest

from revertbench.harness.prompt import PROMPT_VERSION, TOOL_DEFINITIONS, TaskPrompt, build_messages


def test_prompt_contains_only_explicit_bad_task_fields() -> None:
    task = TaskPrompt(
        episode_id="episode-001",
        bad_pr_title="Make indexing faster",
        bad_diff="diff --git a/project.py b/project.py\n+return len(items)\n",
    )
    messages = build_messages(task)
    assert [message["role"] for message in messages] == ["system", "user"]
    payload = json.loads(messages[1]["content"])
    assert payload == {
        "episode": "episode-001",
        "bad_pr_title": task.bad_pr_title,
        "bad_diff": task.bad_diff,
        "pr_body_policy": "omitted_all_cases_v1",
    }
    assert "15 model calls" in messages[0]["content"]
    assert "bad repository only" in messages[0]["content"]
    assert "write_test" in messages[0]["content"]
    assert "untrusted" in messages[0]["content"]


def test_prompt_explains_common_policy_without_reference_examples() -> None:
    system = build_messages(TaskPrompt("episode-002", "Title", "diff"))[0]["content"]
    for phrase in (
        "test_",
        "assert",
        "pytest.raises",
        "UTF-8",
        "65,536",
        "no fixtures",
        "no classes",
        "no comprehensions",
        "no reflection",
        "no filesystem",
        "repaired",
        "source inspection",
        "run_tests",
        "submit",
    ):
        assert phrase in system
    assert "snicker" not in system
    assert "10**30" not in system


@pytest.mark.parametrize("identity", ["", "case-fixsha", "../episode-1", "episode-1\nsecret"])
def test_prompt_rejects_nonopaque_identity(identity: str) -> None:
    with pytest.raises(ValueError, match="opaque"):
        TaskPrompt(identity, "Title", "diff")


@pytest.mark.parametrize("title,diff", [("x" * 4097, "diff"), ("Title", "x" * 131073)])
def test_prompt_bounds_inputs_without_silent_truncation(title: str, diff: str) -> None:
    with pytest.raises(ValueError, match="limit"):
        TaskPrompt("episode-001", title, diff)


def test_tool_contract_is_small_and_requires_explicit_arguments() -> None:
    expected = {
        "list_dir": {"path"},
        "read_file": {"path", "start", "end"},
        "search": {"pattern", "path"},
        "write_test": {"content"},
        "run_tests": {"node_ids"},
        "submit": set(),
    }
    assert len(TOOL_DEFINITIONS) == len(expected)
    for tool in TOOL_DEFINITIONS:
        assert tool["type"] == "function"
        function = tool["function"]
        schema = function["parameters"]
        assert set(schema["required"]) == expected[function["name"]]
        assert set(schema["properties"]) == expected[function["name"]]
        assert schema["additionalProperties"] is False
    assert "shell" not in json.dumps(TOOL_DEFINITIONS)


def test_policy_feedback_contract_is_versioned_and_preserves_saved_test_semantics() -> None:
    assert PROMPT_VERSION == "bad-only-policy-feedback-v2"
    system = build_messages(TaskPrompt("episode-001", "Title", "diff"))[0]["content"]
    assert "saved successfully" in system
    assert "policy.allowed" in system
    assert "ineligible" in system and "before pytest" in system
    assert "does not predict" in system
    assert "submit" in system


def test_saved_test_completion_prompt_is_explicit_and_default_is_unchanged() -> None:
    from revertbench.harness.prompt import prompt_version

    task = TaskPrompt("episode-001", "Title", "diff")
    default = build_messages(task)
    assert prompt_version("explicit_submit") == PROMPT_VERSION
    assert build_messages(task, completion_policy="explicit_submit") == default
    variant = build_messages(task, completion_policy="saved_test_on_stop_v1")
    assert prompt_version("saved_test_on_stop_v1") == "saved-test-on-stop-v1"
    assert "Only submit ends" in default[0]["content"]
    assert "normal stop" in variant[0]["content"]
    assert "last successfully saved" in variant[0]["content"]
    assert "Only submit ends" not in variant[0]["content"]
    assert variant[1] == default[1]
    assert "budget" in variant[0]["content"] and "no test is extracted" in variant[0]["content"]
