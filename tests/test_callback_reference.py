"""Static admission of a plain delivery callback; no target code is executed."""

import ast
from pathlib import Path

import pytest

from revertbench.grade.policy import check_submission
from revertbench.mine.reference import extract_reference

# An independent toy models a delivery observation through a plain callback.
# Exact historical function preservation is checked in private case evidence.
_FUNCTION = """def test_notifications() -> None:
    total = 0

    def record(message: t.Any) -> None:
        nonlocal total
        total += 1

    channel = dispatch_api.Channel()
    channel.attach(record, message=917263584)
    channel.emit(917263584)
    assert total == 1
    channel.emit(int("917263584"))
    assert total == 2
"""
_SOURCE = (
    "from __future__ import annotations\nimport typing as t\nimport dispatch_api\n\n" + _FUNCTION
)


@pytest.fixture
def callback_project(tmp_path: Path) -> Path:
    (tmp_path / "dispatch_api.py").write_text(
        "class Channel:\n"
        "    def attach(self, receiver, message):\n        return receiver\n"
        "    def emit(self, message):\n        return []\n",
        encoding="utf-8",
    )
    return tmp_path


def test_plain_delivery_callback_and_annotations_are_admitted(callback_project: Path) -> None:
    result = check_submission(_SOURCE, callback_project)
    assert result.allowed, result


def test_extraction_preserves_whole_function_future_flag_and_needed_typing(
    callback_project: Path,
) -> None:
    source = _SOURCE + "\nrecord = 'unrelated module constant'\nUNUSED = 17\n"
    extracted = extract_reference(source, "test_notifications", bad_tree=callback_project)
    assert ast.dump(ast.parse(extracted).body[-1]) == ast.dump(ast.parse(_FUNCTION).body[0])
    assert extracted.startswith("from __future__ import annotations\n")
    assert "import typing as t" in extracted
    assert "unrelated module constant" not in extracted
    assert "UNUSED" not in extracted
    assert sum(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(extracted))) == 2


def test_nested_parameter_does_not_hide_its_outer_annotation_import(
    callback_project: Path,
) -> None:
    source = _SOURCE.replace("message: t.Any", "t: t.Any")
    extracted = extract_reference(source, "test_notifications", bad_tree=callback_project)
    assert "import typing as t" in extracted
    assert "def record(t: t.Any) -> None:" in extracted


def test_extraction_does_not_confuse_local_callback_with_same_named_module_binding() -> None:
    source = _SOURCE + "\nrecord = 'module value'\n"
    assert "module value" not in extract_reference(source, "test_notifications")


@pytest.mark.parametrize(
    "source",
    [
        _SOURCE.replace("message: t.Any", "message").replace(" -> None", ""),
        _SOURCE.replace("import typing as t", "import typing as hints").replace(
            "t.Any", "hints.Any"
        ),
        _SOURCE.replace("from __future__ import annotations\n", ""),
    ],
)
def test_callback_annotation_variants_grant_no_additional_permissions(
    callback_project: Path, source: str
) -> None:
    assert check_submission(source, callback_project).allowed


@pytest.mark.parametrize(
    "before,after",
    [
        ("total = 0", "total = False"),
        ("total = 0", "total = int('0')"),
        ("total = 0", "initial = 0\n    total = initial"),
        ("def record(message: t.Any)", "def record(message: t.Any = None)"),
        ("def record(message: t.Any)", "def record(*message)"),
        ("def record(message: t.Any)", "def record(message, other)"),
        ("def record(message: t.Any)", "async def record(message: t.Any)"),
        ("nonlocal total", "global total"),
        ("nonlocal total", "nonlocal total, other"),
        ("total += 1", "total -= 1"),
        ("total += 1", "total += message"),
        ("total += 1", "total += 2"),
        ("total += 1", "total += True"),
        ("total += 1", "total = 1"),
        ("total += 1", "total += 1\n        return message"),
        ("total += 1", "total += 1\n        def inner():\n            return None"),
        ("def record(message: t.Any) -> None", "def record(message: t.Any) -> int"),
        ("message: t.Any", "message: t.Optional[int]"),
        ("message: t.Any", "message: dispatch_api.Channel"),
        ("message: t.Any", "message: t.Any()"),
        ("def record(message: t.Any)", "@dispatch_api.Channel\n    def record(message: t.Any)"),
    ],
)
def test_callback_scope_is_not_a_general_nested_function_capability(
    callback_project: Path, before: str, after: str
) -> None:
    result = check_submission(_SOURCE.replace(before, after), callback_project)
    assert not result.allowed, result


def test_callback_cannot_capture_a_module_counter(callback_project: Path) -> None:
    source = _SOURCE.replace(
        "def test_notifications() -> None:\n    total = 0",
        "total = 0\n\ndef test_notifications() -> None:",
    )
    assert not check_submission(source, callback_project).allowed


def test_callback_is_not_declared_inside_a_branch(callback_project: Path) -> None:
    source = _SOURCE.replace(
        "    def record(message: t.Any) -> None:\n        nonlocal total\n        total += 1",
        "    if True:\n        def record(message: t.Any) -> None:\n"
        "            nonlocal total\n            total += 1",
    )
    assert not check_submission(source, callback_project).allowed


def test_callback_does_not_enable_direct_submission_helper_calls(callback_project: Path) -> None:
    source = _SOURCE.replace(
        "channel = dispatch_api.Channel()", "record(None)\n    channel = dispatch_api.Channel()"
    )
    assert not check_submission(source, callback_project).allowed


def test_more_than_one_callback_in_the_same_test_is_unsupported(callback_project: Path) -> None:
    source = _SOURCE.replace(
        "    channel = dispatch_api.Channel()",
        "    def second(message: t.Any) -> None:\n"
        "        nonlocal total\n        total += 1\n\n    channel = dispatch_api.Channel()",
    )
    assert not check_submission(source, callback_project).allowed


def test_typing_alias_rebinding_is_not_an_annotation_permission(callback_project: Path) -> None:
    source = _SOURCE.replace("    def record", "    t = 1\n\n    def record")
    assert not check_submission(source, callback_project).allowed


def test_other_typing_import_forms_are_outside_the_annotation_subset(
    callback_project: Path,
) -> None:
    source = _SOURCE.replace("import typing as t", "from typing import Any").replace("t.Any", "Any")
    assert not check_submission(source, callback_project).allowed


def test_nonlocal_outside_the_counter_callback_remains_unsupported(callback_project: Path) -> None:
    source = "def test_behavior():\n    nonlocal total\n    assert True\n"
    assert not check_submission(source, callback_project).allowed


def test_declaring_a_callback_is_not_an_observation(callback_project: Path) -> None:
    source = _SOURCE[: _SOURCE.index("    channel = dispatch_api.Channel()")]
    result = check_submission(source, callback_project)
    assert not result.allowed
    assert result.reason == "empty_test"


def test_callback_does_not_bypass_bad_tree_api_resolution(callback_project: Path) -> None:
    source = _SOURCE + "    assert dispatch_api.fixed_only()\n"
    result = check_submission(source, callback_project)
    assert not result.allowed
    assert result.reason == "unresolved_symbol"


@pytest.mark.parametrize(
    "extra",
    [
        "assert t.Any",
        "assert t.cast(None, 1)",
        "assert record.message",
    ],
)
def test_annotation_and_callback_bindings_are_not_runtime_module_or_member_access(
    callback_project: Path, extra: str
) -> None:
    source = _SOURCE + "    " + extra + "\n"
    assert not check_submission(source, callback_project).allowed


@pytest.mark.parametrize("path", ["typing.py", "src/typing.py", "typing/__init__.py"])
def test_typing_annotations_require_unshadowed_stdlib_module(
    callback_project: Path, path: str
) -> None:
    module = callback_project / path
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("Any = 1\n", encoding="utf-8")
    assert not check_submission(_SOURCE, callback_project).allowed


def test_typing_submodule_is_not_an_alternate_project_api(callback_project: Path) -> None:
    package = callback_project / "typing"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "extras.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    source = "import typing.extras as hints\ndef test_api():\n    assert hints.value() == 1\n"
    assert not check_submission(source, callback_project).allowed


def test_future_flag_does_not_add_unrelated_future_features(callback_project: Path) -> None:
    assert not check_submission(
        _SOURCE.replace("__future__ import annotations", "__future__ import division"),
        callback_project,
    ).allowed


def test_unannotated_legacy_extraction_output_is_unchanged() -> None:
    source = (
        "from __future__ import annotations\nimport dispatch_api\n"
        "def test_api():\n    assert dispatch_api.Channel()\n"
    )
    assert extract_reference(source, "test_api") == (
        "import dispatch_api\n\ndef test_api():\n    assert dispatch_api.Channel()\n"
    )
